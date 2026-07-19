"""Core batch-download logic for the Streamlit media downloader.

Use only with media you own, public-domain material, or content for which you
have explicit permission. This module does not bypass authentication, DRM, or
other access controls.
"""

from __future__ import annotations

import re
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

import yt_dlp
from yt_dlp.utils import download_range_func

CONCURRENT_FRAGMENTS = 4
PARALLEL_DOWNLOADS = 2  # Different URLs only; jobs for one URL run sequentially.
MAX_BATCH_JOBS = 10
SOCKET_TIMEOUT_SECONDS = 30
DOWNLOAD_RETRIES = 5
FRAGMENT_RETRIES = 5

LOG_LOCK = threading.Lock()
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
        # Keep the Streamlit status area concise.
        if level in {"warning", "error"}:
            _log(f"Job {self.job_number}: {clean}", self.callback)

    def debug(self, message: str) -> None:
        # yt-dlp sometimes sends informational output through debug().
        if str(message).startswith("[debug]"):
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
    """Convert low-level yt-dlp failures into clear web-app messages."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", str(message)).strip()
    clean = re.sub(r"^ERROR:\s*", "", clean, flags=re.IGNORECASE)
    lower = clean.lower()

    if "sign in to confirm" in lower or "not a bot" in lower:
        return (
            "YouTube rejected the Streamlit Cloud server IP and requested "
            "human verification. This is a hosting/network restriction, not "
            "a timestamp error. Try again later or run the app locally."
        )
    if "requested format is not available" in lower:
        return (
            "The requested MP4 format is unavailable for this video. "
            "Update yt-dlp or try another output type."
        )
    if "http error 403" in lower or "403 forbidden" in lower:
        return (
            "The media server returned HTTP 403. The cloud IP or selected "
            "stream was refused. Try again later or run the app locally."
        )
    if "js runtime" in lower or "javascript runtime" in lower or "challenge solver" in lower:
        return (
            "YouTube's JavaScript challenge could not be solved. Confirm that "
            "yt-dlp[default] and a supported JavaScript runtime are installed."
        )
    return clean or "Unknown yt-dlp failure."


def parse_urls(raw_urls: str) -> list[str]:
    """Return unique HTTP(S) URLs supplied with spaces, commas, or new lines."""
    values = [
        value.strip()
        for value in re.split(r"[,\s]+", raw_urls)
        if value.strip()
    ]

    unique_urls: list[str] = []
    seen: set[str] = set()
    for url in values:
        validate_url(url)
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    return unique_urls


def validate_url(url: str) -> None:
    """Validate the minimum URL format accepted by yt-dlp."""
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"Invalid URL: {url}")


def timestamp_to_seconds(value: str) -> float:
    """Convert SS, MM:SS, or HH:MM:SS into non-negative seconds."""
    raw_value = str(value).strip()
    if not raw_value:
        raise ValueError("Timestamp cannot be empty.")

    parts = raw_value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(
            f"Invalid timestamp '{value}'. Use SS, MM:SS, or HH:MM:SS."
        ) from exc

    if len(numbers) == 1:
        total_seconds = numbers[0]
    elif len(numbers) == 2:
        minutes, seconds = numbers
        total_seconds = minutes * 60 + seconds
    elif len(numbers) == 3:
        hours, minutes, seconds = numbers
        total_seconds = hours * 3600 + minutes * 60 + seconds
    else:
        raise ValueError(
            f"Invalid timestamp '{value}'. Use SS, MM:SS, or HH:MM:SS."
        )

    if total_seconds < 0:
        raise ValueError("Timestamp cannot be negative.")
    return total_seconds


def seconds_to_filename_time(seconds: float) -> str:
    """Convert seconds to a filename-safe HH-MM-SS-mmm value."""
    milliseconds_total = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}-{minutes:02d}-{secs:02d}-{milliseconds:03d}"


def _log(message: str, callback: LogCallback | None) -> None:
    if callback is None:
        return
    with LOG_LOCK:
        callback(message)


def build_batch_jobs(job_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate web-table rows and convert them into yt-dlp jobs.

    Each row may independently select its download type, scope, and timestamps.
    Blank rows are ignored.
    """
    jobs: list[dict[str, Any]] = []
    seen_specs: set[tuple[Any, ...]] = set()

    for row_number, row in enumerate(job_rows, start=1):
        url = str(row.get("url", "")).strip()
        if not url:
            continue

        validate_url(url)

        download_type = str(row.get("download_type", "video")).strip().lower()
        if download_type not in VALID_DOWNLOAD_TYPES:
            raise ValueError(
                f"Row {row_number}: unsupported download type '{download_type}'."
            )

        scope = str(row.get("scope", "full")).strip().lower()
        if scope not in VALID_SCOPES:
            raise ValueError(f"Row {row_number}: scope must be full or segment.")

        # Thumbnails do not have a meaningful segment mode.
        is_segment = scope == "segment" and download_type != "thumbnail"
        start: float | None = None
        end: float | None = None

        if is_segment:
            start = timestamp_to_seconds(str(row.get("start", "")))
            end = timestamp_to_seconds(str(row.get("end", "")))
            if end <= start:
                raise ValueError(
                    f"Row {row_number}: end timestamp must be greater than start."
                )

        specification = (url, download_type, is_segment, start, end)
        if specification in seen_specs:
            continue
        seen_specs.add(specification)

        jobs.append(
            {
                "job_number": len(jobs) + 1,
                "url": url,
                "download_type": download_type,
                "is_segment": is_segment,
                "start": start,
                "end": end,
            }
        )

    if not jobs:
        raise ValueError("Add at least one valid URL to the batch table.")
    if len(jobs) > MAX_BATCH_JOBS:
        raise ValueError(
            f"The public app accepts a maximum of {MAX_BATCH_JOBS} jobs per run."
        )
    return jobs


def build_jobs(
    urls: list[str],
    download_type: str,
    scope: str,
    start_text: str = "",
    end_text: str = "",
) -> list[dict[str, Any]]:
    """Compatibility helper for one shared configuration across many URLs."""
    return build_batch_jobs(
        {
            "url": url,
            "download_type": download_type,
            "scope": scope,
            "start": start_text,
            "end": end_text,
        }
        for url in urls
    )


def get_scope_label(job: dict[str, Any]) -> str:
    if not job["is_segment"]:
        return f"job-{job['job_number']:03d}_full"
    return (
        f"job-{job['job_number']:03d}_"
        f"from-{seconds_to_filename_time(job['start'])}_"
        f"to-{seconds_to_filename_time(job['end'])}"
    )


def build_output_template(job: dict[str, Any], output_dir: Path) -> str:
    scope_label = get_scope_label(job)
    filename = (
        "%(title).80s_%(id)s_"
        f"{scope_label}_{job['download_type']}.%(ext)s"
    )
    return str(output_dir / filename)


def build_common_options(job: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    return {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        # Node is installed through packages.txt. It is not enabled by default
        # by current yt-dlp releases, so enable it explicitly.
        "js_runtimes": {"node": {}},
        # Permit yt-dlp to obtain its official EJS challenge scripts when the
        # matching Python package is temporarily unavailable or stale.
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
        "overwrites": False,
        "restrictfilenames": True,
        "prefer_ffmpeg": True,
        "outtmpl": build_output_template(job, output_dir),
    }


def build_download_options(job: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    options = build_common_options(job, output_dir)

    if job["is_segment"]:
        options["download_ranges"] = download_range_func(
            None,
            [(job["start"], job["end"])],
        )
        options["force_keyframes_at_cuts"] = False

    download_type = job["download_type"]
    if download_type == "video":
        options.update(
            {
                "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "merge_output_format": "mp4",
            }
        )
    elif download_type == "silent":
        options.update(
            {
                "format": "bv*[ext=mp4]/bv*",
                "merge_output_format": "mp4",
            }
        )
    elif download_type == "audio":
        options.update(
            {
                "format": "ba/b",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    elif download_type == "thumbnail":
        options.update(
            {
                "skip_download": True,
                "writethumbnail": True,
                "postprocessors": [
                    {
                        "key": "FFmpegThumbnailsConvertor",
                        "format": "jpg",
                    }
                ],
            }
        )
    return options


def download_job(
    job: dict[str, Any],
    output_dir: Path,
    log_callback: LogCallback | None = None,
) -> dict[str, Any]:
    """Run one independent yt-dlp job and return a structured result."""
    job_number = job["job_number"]
    _log(f"Job {job_number}: starting", log_callback)

    # Isolate each job completely. This prevents fragments and temporary files
    # from multiple segments of the same video affecting one another.
    job_output_dir = output_dir / f"job-{job_number:03d}"
    job_output_dir.mkdir(parents=True, exist_ok=True)

    logger = YtDlpJobLogger(job_number, log_callback)
    options = build_download_options(job, job_output_dir)
    options["logger"] = logger

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result_code = downloader.download([job["url"]])
        if result_code != 0:
            raise RuntimeError(f"yt-dlp returned exit code {result_code}")

        produced_files = [
            path for path in job_output_dir.rglob("*")
            if path.is_file()
            and path.suffix not in {".part", ".ytdl"}
            and not path.name.endswith(".temp")
        ]
        if not produced_files:
            raise RuntimeError("yt-dlp completed without producing an output file.")

        _log(f"Job {job_number}: completed", log_callback)
        return {
            "job_number": job_number,
            "url": job["url"],
            "download_type": job["download_type"],
            "success": True,
            "error": None,
        }
    except Exception as exc:  # yt-dlp raises extractor-specific exceptions.
        error_message = logger.best_error(exc)
        _log(f"Job {job_number}: failed - {error_message}", log_callback)
        return {
            "job_number": job_number,
            "url": job["url"],
            "download_type": job["download_type"],
            "success": False,
            "error": error_message,
        }


def run_jobs(
    jobs: list[dict[str, Any]],
    output_dir: Path,
    log_callback: LogCallback | None = None,
) -> list[dict[str, Any]]:
    """Run different URLs concurrently but serialize jobs for the same URL.

    Four segments from one YouTube video therefore reuse a conservative access
    pattern instead of opening several simultaneous requests to that video.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        grouped.setdefault(job["url"], []).append(job)

    def run_url_group(url_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            download_job(job, output_dir, log_callback)
            for job in url_jobs
        ]

    worker_count = min(PARALLEL_DOWNLOADS, len(grouped))
    if worker_count <= 1:
        results = []
        for url_jobs in grouped.values():
            results.extend(run_url_group(url_jobs))
        return sorted(results, key=lambda item: item["job_number"])

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(run_url_group, url_jobs): url
            for url, url_jobs in grouped.items()
        }
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as exc:
                # This should be rare because download_job handles job-level
                # failures, but preserve a useful group-level diagnostic.
                url = futures[future]
                for job in grouped[url]:
                    results.append({
                        "job_number": job["job_number"],
                        "url": job["url"],
                        "download_type": job["download_type"],
                        "success": False,
                        "error": normalise_download_error(str(exc)),
                    })

    return sorted(results, key=lambda item: item["job_number"])


def create_batch_download_archive(
    job_rows: Iterable[dict[str, Any]],
    log_callback: LogCallback | None = None,
) -> tuple[Path, list[dict[str, Any]], Path]:
    """Download a validated batch and package all successful files in one ZIP."""
    jobs = build_batch_jobs(job_rows)
    session_dir = Path("runtime") / str(uuid.uuid4())
    output_dir = session_dir / "downloads"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = run_jobs(jobs, output_dir, log_callback)
    files = [
        path
        for path in output_dir.rglob("*")
        if path.is_file()
        and path.suffix not in {".part", ".ytdl"}
        and not path.name.endswith(".temp")
    ]

    if not files:
        errors = "; ".join(
            str(result.get("error") or "Job failed without a diagnostic.")
            for result in results
            if not result["success"]
        )
        shutil.rmtree(session_dir, ignore_errors=True)
        raise RuntimeError(errors or "No downloadable files were produced.")

    archive_path = session_dir / "downloaded_media_batch.zip"
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
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
    """Compatibility wrapper for shared settings across several URLs."""
    urls = parse_urls(raw_urls)
    rows = [
        {
            "url": url,
            "download_type": download_type,
            "scope": scope,
            "start": start_text,
            "end": end_text,
        }
        for url in urls
    ]
    return create_batch_download_archive(rows, log_callback)


def cleanup_session(session_dir: Path) -> None:
    """Delete one request's temporary downloads and ZIP archive."""
    shutil.rmtree(session_dir, ignore_errors=True)
