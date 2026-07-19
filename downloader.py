"""Core download logic for the Streamlit media downloader.

Use only with videos that you own, that are licensed for download, or for which
permission has been granted. This module does not bypass authentication, DRM,
or access controls.
"""

from __future__ import annotations

import re
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.utils import download_range_func

CONCURRENT_FRAGMENTS = 4
PARALLEL_DOWNLOADS = 2
SOCKET_TIMEOUT_SECONDS = 30
DOWNLOAD_RETRIES = 5
FRAGMENT_RETRIES = 5

LOG_LOCK = threading.Lock()
LogCallback = Callable[[str], None]


def parse_urls(raw_urls: str) -> list[str]:
    """Return unique HTTP(S) URLs supplied with spaces, commas, or new lines."""
    urls = [
        value.strip()
        for value in re.split(r"[,\s]+", raw_urls)
        if value.strip()
    ]

    unique_urls: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"Invalid URL: {url}")
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    return unique_urls


def timestamp_to_seconds(value: str) -> float:
    """Convert SS, MM:SS, or HH:MM:SS into non-negative seconds."""
    raw_value = value.strip()
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


def build_jobs(
    urls: list[str],
    download_type: str,
    scope: str,
    start_text: str = "",
    end_text: str = "",
) -> list[dict[str, Any]]:
    """Create independent download jobs from validated web-form values."""
    if download_type not in {"video", "silent", "audio", "thumbnail"}:
        raise ValueError("Unsupported download type.")

    is_segment = scope == "segment" and download_type != "thumbnail"
    start: float | None = None
    end: float | None = None

    if is_segment:
        start = timestamp_to_seconds(start_text)
        end = timestamp_to_seconds(end_text)
        if end <= start:
            raise ValueError("End timestamp must be greater than start timestamp.")

    return [
        {
            "job_number": number,
            "url": url,
            "download_type": download_type,
            "is_segment": is_segment,
            "start": start,
            "end": end,
        }
        for number, url in enumerate(urls, start=1)
    ]


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
    type_label = job["download_type"]
    filename = (
        "%(title).80s_%(id)s_"
        f"{scope_label}_{type_label}.%(ext)s"
    )
    return str(output_dir / filename)


def build_common_options(job: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    return {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
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
    """Run one yt-dlp job and return a structured result."""
    job_number = job["job_number"]
    _log(f"Job {job_number}: starting", log_callback)

    options = build_download_options(job, output_dir)
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result_code = downloader.download([job["url"]])
        if result_code != 0:
            raise RuntimeError(f"yt-dlp returned exit code {result_code}")
        _log(f"Job {job_number}: completed", log_callback)
        return {"job_number": job_number, "success": True, "error": None}
    except Exception as exc:  # yt-dlp raises several extractor-specific exceptions.
        _log(f"Job {job_number}: failed - {exc}", log_callback)
        return {"job_number": job_number, "success": False, "error": str(exc)}


def run_jobs(
    jobs: list[dict[str, Any]],
    output_dir: Path,
    log_callback: LogCallback | None = None,
) -> list[dict[str, Any]]:
    worker_count = min(PARALLEL_DOWNLOADS, len(jobs))
    if worker_count <= 1:
        return [download_job(job, output_dir, log_callback) for job in jobs]

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(download_job, job, output_dir, log_callback): job
            for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    {
                        "job_number": job["job_number"],
                        "success": False,
                        "error": str(exc),
                    }
                )
    return sorted(results, key=lambda item: item["job_number"])


def create_download_archive(
    raw_urls: str,
    download_type: str,
    scope: str,
    start_text: str = "",
    end_text: str = "",
    log_callback: LogCallback | None = None,
) -> tuple[Path, list[dict[str, Any]], Path]:
    """Download requested media and create a ZIP suitable for Streamlit."""
    urls = parse_urls(raw_urls)
    if not urls:
        raise ValueError("Enter at least one URL.")
    if len(urls) > 5:
        raise ValueError("The public app accepts a maximum of five URLs per run.")

    jobs = build_jobs(urls, download_type, scope, start_text, end_text)
    session_dir = Path("runtime") / str(uuid.uuid4())
    output_dir = session_dir / "downloads"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = run_jobs(jobs, output_dir, log_callback)
    files = [path for path in output_dir.iterdir() if path.is_file() and path.suffix != ".part"]

    if not files:
        errors = "; ".join(
            result["error"] or "unknown error"
            for result in results
            if not result["success"]
        )
        shutil.rmtree(session_dir, ignore_errors=True)
        raise RuntimeError(errors or "No downloadable files were produced.")

    archive_path = session_dir / "downloaded_media.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)

    return archive_path, results, session_dir


def cleanup_session(session_dir: Path) -> None:
    """Delete one request's temporary downloads and ZIP archive."""
    shutil.rmtree(session_dir, ignore_errors=True)
