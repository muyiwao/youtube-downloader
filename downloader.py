"""Core batch-download logic for the Streamlit media downloader.

Use only with media you own, public-domain material, or content for which you
have explicit permission. This module does not bypass authentication, DRM, or
other access controls.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

import yt_dlp

CONCURRENT_FRAGMENTS = 4
PARALLEL_DOWNLOADS = 2
MAX_BATCH_JOBS = 10
SOCKET_TIMEOUT_SECONDS = 30
DOWNLOAD_RETRIES = 5
FRAGMENT_RETRIES = 5

# A known supported Deno release. yt-dlp currently requires Deno 2.3.0+.
DENO_VERSION = "2.3.7"
TOOLS_DIR = Path("runtime_tools")

LOG_LOCK = threading.Lock()
RUNTIME_LOCK = threading.Lock()
LogCallback = Callable[[str], None]

VALID_DOWNLOAD_TYPES = {"video", "silent", "audio", "thumbnail"}
VALID_SCOPES = {"full", "segment"}


class YtDlpJobLogger:
    """Capture yt-dlp diagnostics so web users receive useful errors."""

    def __init__(self, job_number: int, callback: LogCallback | None) -> None:
        self.job_number = job_number
        self.callback = callback
        self.messages: list[str] = []

    def _record(self, level: str, message: str) -> None:
        clean = str(message or "").strip()
        if not clean:
            return
        self.messages.append(clean)
        if level in {"warning", "error"}:
            _log(f"Job {self.job_number}: {clean}", self.callback)

    def debug(self, message: str) -> None:
        return

    def info(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        self._record("warning", message)

    def error(self, message: str) -> None:
        self._record("error", message)

    def best_error(self, fallback: BaseException | None = None) -> str:
        for message in reversed(self.messages):
            if message:
                return normalise_download_error(message)
        fallback_text = str(fallback or "").strip()
        return normalise_download_error(fallback_text or "Unknown yt-dlp failure.")


def normalise_download_error(message: str) -> str:
    """Convert low-level yt-dlp/FFmpeg failures into clear web messages."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", str(message)).strip()
    clean = re.sub(r"^ERROR:\s*", "", clean, flags=re.IGNORECASE)
    lower = clean.lower()

    if "sign in to confirm" in lower or "not a bot" in lower:
        return (
            "YouTube rejected the Streamlit Cloud server IP and requested "
            "human verification. Try again later or run the app locally."
        )
    if "requested format is not available" in lower:
        return "The requested media format is unavailable for this video."
    if "http error 403" in lower or "403 forbidden" in lower:
        return (
            "The media server returned HTTP 403. The cloud IP or media stream "
            "was refused. Try again later or run the app locally."
        )
    if "no supported javascript runtime" in lower or "js runtime" in lower:
        return (
            "No supported JavaScript runtime was available. The app attempted "
            "to install Deno automatically; check the deployment logs."
        )
    if "ffmpeg exited with code" in lower:
        return (
            "FFmpeg could not process the media. The revised app downloads the "
            "source first and cuts segments locally to avoid remote range errors."
        )
    return clean or "Unknown media download failure."


def _log(message: str, callback: LogCallback | None) -> None:
    if callback is None:
        return
    with LOG_LOCK:
        callback(message)


def _version_tuple(raw: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _runtime_version(executable: str, argument: str = "--version") -> tuple[int, ...]:
    try:
        result = subprocess.run(
            [executable, argument], capture_output=True, text=True, timeout=10, check=False
        )
        return _version_tuple(f"{result.stdout}\n{result.stderr}")
    except (OSError, subprocess.SubprocessError):
        return (0, 0, 0)


def _download_deno() -> Path:
    """Install a private Deno binary for Streamlit Cloud when none is available."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system != "linux" or machine not in {"x86_64", "amd64"}:
        raise RuntimeError(
            "Automatic Deno installation currently supports Linux x86_64 only. "
            "Install Deno 2.3+ or Node 22+ manually on this host."
        )

    install_dir = TOOLS_DIR / f"deno-{DENO_VERSION}"
    executable = install_dir / "deno"
    if executable.exists() and _runtime_version(str(executable)) >= (2, 3, 0):
        return executable.resolve()

    install_dir.mkdir(parents=True, exist_ok=True)
    archive_path = install_dir / "deno.zip"
    url = (
        "https://github.com/denoland/deno/releases/download/"
        f"v{DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip"
    )

    try:
        urllib.request.urlretrieve(url, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extract("deno", install_dir)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception as exc:
        shutil.rmtree(install_dir, ignore_errors=True)
        raise RuntimeError(f"Could not install the Deno runtime: {exc}") from exc
    finally:
        archive_path.unlink(missing_ok=True)

    if _runtime_version(str(executable)) < (2, 3, 0):
        raise RuntimeError("The downloaded Deno executable is not a supported version.")
    return executable.resolve()


def get_js_runtimes(log_callback: LogCallback | None = None) -> dict[str, dict[str, str]]:
    """Return an explicitly configured, supported JavaScript runtime."""
    with RUNTIME_LOCK:
        deno = shutil.which("deno")
        if deno and _runtime_version(deno) >= (2, 3, 0):
            return {"deno": {"path": deno}}

        node = shutil.which("node")
        if node and _runtime_version(node, "--version") >= (22, 0, 0):
            return {"node": {"path": node}}

        _log("Installing a private Deno runtime for YouTube extraction...", log_callback)
        deno_path = _download_deno()
        _log(f"Deno {DENO_VERSION} is ready.", log_callback)
        return {"deno": {"path": str(deno_path)}}


def parse_urls(raw_urls: str) -> list[str]:
    values = [value.strip() for value in re.split(r"[,\s]+", raw_urls) if value.strip()]
    unique_urls: list[str] = []
    seen: set[str] = set()
    for url in values:
        validate_url(url)
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    return unique_urls


def validate_url(url: str) -> None:
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"Invalid URL: {url}")


def timestamp_to_seconds(value: str) -> float:
    raw_value = str(value).strip()
    if not raw_value:
        raise ValueError("Timestamp cannot be empty.")
    parts = raw_value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp '{value}'. Use SS, MM:SS, or HH:MM:SS.") from exc

    if len(numbers) == 1:
        total_seconds = numbers[0]
    elif len(numbers) == 2:
        total_seconds = numbers[0] * 60 + numbers[1]
    elif len(numbers) == 3:
        total_seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    else:
        raise ValueError(f"Invalid timestamp '{value}'. Use SS, MM:SS, or HH:MM:SS.")
    if total_seconds < 0:
        raise ValueError("Timestamp cannot be negative.")
    return total_seconds


def seconds_to_filename_time(seconds: float) -> str:
    milliseconds_total = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}-{minutes:02d}-{secs:02d}-{milliseconds:03d}"


def build_batch_jobs(job_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen_specs: set[tuple[Any, ...]] = set()
    for row_number, row in enumerate(job_rows, start=1):
        url = str(row.get("url", "")).strip()
        if not url:
            continue
        validate_url(url)
        download_type = str(row.get("download_type", "video")).strip().lower()
        if download_type not in VALID_DOWNLOAD_TYPES:
            raise ValueError(f"Row {row_number}: unsupported download type '{download_type}'.")
        scope = str(row.get("scope", "full")).strip().lower()
        if scope not in VALID_SCOPES:
            raise ValueError(f"Row {row_number}: scope must be full or segment.")

        is_segment = scope == "segment" and download_type != "thumbnail"
        start: float | None = None
        end: float | None = None
        if is_segment:
            start = timestamp_to_seconds(str(row.get("start", "")))
            end = timestamp_to_seconds(str(row.get("end", "")))
            if end <= start:
                raise ValueError(f"Row {row_number}: end timestamp must be greater than start.")

        specification = (url, download_type, is_segment, start, end)
        if specification in seen_specs:
            continue
        seen_specs.add(specification)
        jobs.append({
            "job_number": len(jobs) + 1,
            "url": url,
            "download_type": download_type,
            "is_segment": is_segment,
            "start": start,
            "end": end,
        })

    if not jobs:
        raise ValueError("Add at least one valid URL to the batch table.")
    if len(jobs) > MAX_BATCH_JOBS:
        raise ValueError(f"The public app accepts a maximum of {MAX_BATCH_JOBS} jobs per run.")
    return jobs


def build_jobs(urls: list[str], download_type: str, scope: str, start_text: str = "", end_text: str = "") -> list[dict[str, Any]]:
    return build_batch_jobs({
        "url": url,
        "download_type": download_type,
        "scope": scope,
        "start": start_text,
        "end": end_text,
    } for url in urls)


def get_scope_label(job: dict[str, Any]) -> str:
    if not job["is_segment"]:
        return f"job-{job['job_number']:03d}_full"
    return (
        f"job-{job['job_number']:03d}_"
        f"from-{seconds_to_filename_time(job['start'])}_"
        f"to-{seconds_to_filename_time(job['end'])}"
    )


def build_output_template(job: dict[str, Any], output_dir: Path) -> str:
    return str(output_dir / f"%(title).80s_%(id)s_{get_scope_label(job)}_{job['download_type']}.%(ext)s")


def build_common_options(
    job: dict[str, Any],
    output_dir: Path,
    js_runtimes: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "js_runtimes": js_runtimes,
        "remote_components": {"ejs:github"},
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "continuedl": True,
        "nopart": False,
        "retries": DOWNLOAD_RETRIES,
        "fragment_retries": FRAGMENT_RETRIES,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "skip_unavailable_fragments": True,
        "overwrites": True,
        "restrictfilenames": True,
        "prefer_ffmpeg": True,
        "outtmpl": build_output_template(job, output_dir),
    }


def build_download_options(
    job: dict[str, Any],
    output_dir: Path,
    js_runtimes: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Build options for full downloads. Segments are cut locally afterwards."""
    options = build_common_options(job, output_dir, js_runtimes)
    download_type = job["download_type"]
    if download_type == "video":
        options.update({
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4",
        })
    elif download_type == "silent":
        options.update({"format": "bv*[ext=mp4]/bv*", "merge_output_format": "mp4"})
    elif download_type == "audio":
        options.update({
            "format": "ba/b",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    elif download_type == "thumbnail":
        options.update({
            "skip_download": True,
            "writethumbnail": True,
            "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
        })
    return options


def _produced_files(directory: Path) -> list[Path]:
    return [
        path for path in directory.rglob("*")
        if path.is_file()
        and path.suffix not in {".part", ".ytdl"}
        and not path.name.endswith(".temp")
    ]


def _download_source_once(
    url: str,
    source_dir: Path,
    js_runtimes: dict[str, dict[str, str]],
    logger: YtDlpJobLogger,
) -> Path:
    """Download one reusable audiovisual source for all segments of one URL."""
    source_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "logger": logger,
        "js_runtimes": js_runtimes,
        "remote_components": {"ejs:github"},
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "retries": DOWNLOAD_RETRIES,
        "fragment_retries": FRAGMENT_RETRIES,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "overwrites": True,
        "restrictfilenames": True,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(source_dir / "source.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        result_code = downloader.download([url])
    if result_code != 0:
        raise RuntimeError(f"yt-dlp returned exit code {result_code}")

    files = _produced_files(source_dir)
    media_files = [path for path in files if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}]
    if not media_files:
        raise RuntimeError("The source download completed without a media file.")
    return max(media_files, key=lambda path: path.stat().st_size)


def _cut_segment(job: dict[str, Any], source_path: Path, output_dir: Path) -> Path:
    """Cut a segment from a local source, avoiding FFmpeg remote URL failures."""
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = float(job["end"] - job["start"])
    stem = get_scope_label(job)

    if job["download_type"] == "audio":
        output_path = output_dir / f"{stem}_audio.mp3"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(job["start"]), "-i", str(source_path), "-t", str(duration),
            "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(output_path),
        ]
    elif job["download_type"] == "silent":
        output_path = output_dir / f"{stem}_silent.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(job["start"]), "-i", str(source_path), "-t", str(duration),
            "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path),
        ]
    else:
        output_path = output_dir / f"{stem}_video.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(job["start"]), "-i", str(source_path), "-t", str(duration),
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output_path),
        ]

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "FFmpeg failed without output.").strip()
        raise RuntimeError(f"FFmpeg segment processing failed: {detail[-1500:]}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg completed without producing the segment file.")
    return output_path


def download_job(
    job: dict[str, Any],
    output_dir: Path,
    js_runtimes: dict[str, dict[str, str]],
    log_callback: LogCallback | None = None,
) -> dict[str, Any]:
    job_number = job["job_number"]
    _log(f"Job {job_number}: starting", log_callback)
    job_output_dir = output_dir / f"job-{job_number:03d}"
    job_output_dir.mkdir(parents=True, exist_ok=True)
    logger = YtDlpJobLogger(job_number, log_callback)
    options = build_download_options(job, job_output_dir, js_runtimes)
    options["logger"] = logger

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result_code = downloader.download([job["url"]])
        if result_code != 0:
            raise RuntimeError(f"yt-dlp returned exit code {result_code}")
        if not _produced_files(job_output_dir):
            raise RuntimeError("yt-dlp completed without producing an output file.")
        _log(f"Job {job_number}: completed", log_callback)
        return {"job_number": job_number, "url": job["url"], "download_type": job["download_type"], "success": True, "error": None}
    except Exception as exc:
        error_message = logger.best_error(exc)
        _log(f"Job {job_number}: failed - {error_message}", log_callback)
        return {"job_number": job_number, "url": job["url"], "download_type": job["download_type"], "success": False, "error": error_message}


def run_jobs(
    jobs: list[dict[str, Any]],
    output_dir: Path,
    log_callback: LogCallback | None = None,
) -> list[dict[str, Any]]:
    """Run different URLs concurrently and reuse one source for URL segments."""
    js_runtimes = get_js_runtimes(log_callback)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        grouped.setdefault(job["url"], []).append(job)

    def run_url_group(url_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        group_results: list[dict[str, Any]] = []
        segment_jobs = [job for job in url_jobs if job["is_segment"]]
        full_jobs = [job for job in url_jobs if not job["is_segment"]]

        # Normal full downloads retain their requested output type.
        for job in full_jobs:
            group_results.append(download_job(job, output_dir, js_runtimes, log_callback))

        if not segment_jobs:
            return group_results

        first_job = segment_jobs[0]
        source_logger = YtDlpJobLogger(first_job["job_number"], log_callback)
        source_dir = output_dir / f"source-{first_job['job_number']:03d}"
        try:
            _log(
                f"Jobs {', '.join(str(job['job_number']) for job in segment_jobs)}: "
                "downloading one reusable source",
                log_callback,
            )
            source_path = _download_source_once(first_job["url"], source_dir, js_runtimes, source_logger)
        except Exception as exc:
            error = source_logger.best_error(exc)
            for job in segment_jobs:
                _log(f"Job {job['job_number']}: failed - {error}", log_callback)
                group_results.append({
                    "job_number": job["job_number"], "url": job["url"],
                    "download_type": job["download_type"], "success": False, "error": error,
                })
            return group_results

        for job in segment_jobs:
            _log(f"Job {job['job_number']}: cutting local segment", log_callback)
            job_dir = output_dir / f"job-{job['job_number']:03d}"
            try:
                _cut_segment(job, source_path, job_dir)
                _log(f"Job {job['job_number']}: completed", log_callback)
                group_results.append({
                    "job_number": job["job_number"], "url": job["url"],
                    "download_type": job["download_type"], "success": True, "error": None,
                })
            except Exception as exc:
                error = normalise_download_error(str(exc))
                _log(f"Job {job['job_number']}: failed - {error}", log_callback)
                group_results.append({
                    "job_number": job["job_number"], "url": job["url"],
                    "download_type": job["download_type"], "success": False, "error": error,
                })

        shutil.rmtree(source_dir, ignore_errors=True)
        return group_results

    worker_count = min(PARALLEL_DOWNLOADS, len(grouped))
    results: list[dict[str, Any]] = []
    if worker_count <= 1:
        for url_jobs in grouped.values():
            results.extend(run_url_group(url_jobs))
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(run_url_group, url_jobs): url for url, url_jobs in grouped.items()}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception as exc:
                    url = futures[future]
                    for job in grouped[url]:
                        results.append({
                            "job_number": job["job_number"], "url": job["url"],
                            "download_type": job["download_type"], "success": False,
                            "error": normalise_download_error(str(exc)),
                        })
    return sorted(results, key=lambda item: item["job_number"])


def create_batch_download_archive(
    job_rows: Iterable[dict[str, Any]],
    log_callback: LogCallback | None = None,
) -> tuple[Path, list[dict[str, Any]], Path]:
    jobs = build_batch_jobs(job_rows)
    session_dir = Path("runtime") / str(uuid.uuid4())
    output_dir = session_dir / "downloads"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = run_jobs(jobs, output_dir, log_callback)
    files = [path for path in output_dir.rglob("*") if path.is_file() and path.suffix not in {".part", ".ytdl"} and not path.name.endswith(".temp")]
    if not files:
        errors = "; ".join(str(result.get("error") or "Job failed without a diagnostic.") for result in results if not result["success"])
        shutil.rmtree(session_dir, ignore_errors=True)
        raise RuntimeError(errors or "No downloadable files were produced.")

    archive_path = session_dir / "downloaded_media_batch.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=str(path.relative_to(output_dir)))
    return archive_path, results, session_dir


def create_download_archive(
    raw_urls: str,
    download_type: str,
    scope: str,
    start_text: str = "",
    end_text: str = "",
    log_callback: LogCallback | None = None,
) -> tuple[Path, list[dict[str, Any]], Path]:
    urls = parse_urls(raw_urls)
    rows = [{"url": url, "download_type": download_type, "scope": scope, "start": start_text, "end": end_text} for url in urls]
    return create_batch_download_archive(rows, log_callback)


def cleanup_session(session_dir: Path) -> None:
    shutil.rmtree(session_dir, ignore_errors=True)
