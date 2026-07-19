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
PARALLEL_DOWNLOADS = 2
MAX_BATCH_JOBS = 10
SOCKET_TIMEOUT_SECONDS = 30
DOWNLOAD_RETRIES = 5
FRAGMENT_RETRIES = 5

LOG_LOCK = threading.Lock()
LogCallback = Callable[[str], None]

VALID_DOWNLOAD_TYPES = {"video", "silent", "audio", "thumbnail"}
VALID_SCOPES = {"full", "segment"}


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

    options = build_download_options(job, output_dir)
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result_code = downloader.download([job["url"]])
        if result_code != 0:
            raise RuntimeError(f"yt-dlp returned exit code {result_code}")
        _log(f"Job {job_number}: completed", log_callback)
        return {
            "job_number": job_number,
            "url": job["url"],
            "download_type": job["download_type"],
            "success": True,
            "error": None,
        }
    except Exception as exc:  # yt-dlp raises extractor-specific exceptions.
        _log(f"Job {job_number}: failed - {exc}", log_callback)
        return {
            "job_number": job_number,
            "url": job["url"],
            "download_type": job["download_type"],
            "success": False,
            "error": str(exc),
        }


def run_jobs(
    jobs: list[dict[str, Any]],
    output_dir: Path,
    log_callback: LogCallback | None = None,
) -> list[dict[str, Any]]:
    """Run independent jobs concurrently, up to PARALLEL_DOWNLOADS workers."""
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
                        "url": job["url"],
                        "download_type": job["download_type"],
                        "success": False,
                        "error": str(exc),
                    }
                )
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
        for path in output_dir.iterdir()
        if path.is_file() and path.suffix != ".part"
    ]

    if not files:
        errors = "; ".join(
            result["error"] or "unknown error"
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
            archive.write(path, arcname=path.name)

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
