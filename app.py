"""Streamlit interface for permitted single and batch media downloads."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from downloader import (
    MAX_BATCH_JOBS,
    cleanup_session,
    create_batch_download_archive,
    parse_urls,
)

st.set_page_config(
    page_title="Video & Audio Downloader",
    page_icon="🎬",
    layout="centered",
)

DOWNLOAD_LABELS = {
    "video": "Video with audio - MP4",
    "silent": "Video only - silent MP4",
    "audio": "Audio only - MP3",
    "thumbnail": "Thumbnail - JPG",
}

BATCH_COLUMNS = ["url", "download_type", "scope", "start", "end"]
COLUMN_ALIASES = {
    "video_url": "url",
    "video url": "url",
    "youtube_url": "url",
    "youtube url": "url",
    "type": "download_type",
    "download type": "download_type",
    "downloadtype": "download_type",
    "start_time": "start",
    "start time": "start",
    "start_timestamp": "start",
    "start timestamp": "start",
    "end_time": "end",
    "end time": "end",
    "end_timestamp": "end",
    "end timestamp": "end",
}

EMPTY_BATCH_ROW = {
    "url": "",
    "download_type": "video",
    "scope": "full",
    "start": "",
    "end": "",
}

CSV_TEMPLATE = """url,download_type,scope,start,end
https://www.youtube.com/watch?v=EXAMPLE1,video,full,,
https://www.youtube.com/watch?v=EXAMPLE2,video,segment,00:00:10,00:00:30
https://www.youtube.com/watch?v=EXAMPLE3,audio,segment,00:01:00,00:02:00
https://www.youtube.com/watch?v=EXAMPLE4,thumbnail,full,,
"""


def normalise_csv_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalise accepted CSV headings and return the five batch columns."""

    if frame.empty:
        raise ValueError("The uploaded CSV file does not contain any rows.")

    renamed: dict[str, str] = {}
    for column in frame.columns:
        normalised = str(column).strip().lower().replace("-", "_")
        renamed[column] = COLUMN_ALIASES.get(normalised, normalised)

    frame = frame.rename(columns=renamed)

    if "url" not in frame.columns:
        raise ValueError(
            "The CSV must contain a 'url' column. Download the template to "
            "see the accepted structure."
        )

    for column in BATCH_COLUMNS:
        if column not in frame.columns:
            frame[column] = EMPTY_BATCH_ROW[column]

    frame = frame[BATCH_COLUMNS].fillna("").astype(str)
    frame["download_type"] = (
        frame["download_type"].str.strip().str.lower().replace("", "video")
    )
    frame["scope"] = frame["scope"].str.strip().str.lower().replace("", "full")

    invalid_types = sorted(
        set(frame["download_type"]) - set(DOWNLOAD_LABELS)
    )
    if invalid_types:
        raise ValueError(
            "Unsupported download_type value(s): "
            f"{', '.join(invalid_types)}. Use video, silent, audio, or thumbnail."
        )

    invalid_scopes = sorted(set(frame["scope"]) - {"full", "segment"})
    if invalid_scopes:
        raise ValueError(
            "Unsupported scope value(s): "
            f"{', '.join(invalid_scopes)}. Use full or segment."
        )

    frame.loc[frame["download_type"] == "thumbnail", "scope"] = "full"
    frame.loc[frame["download_type"] == "thumbnail", ["start", "end"]] = ""

    # Blank rows are allowed in the editor, but do not count against the limit.
    populated_count = int(frame["url"].str.strip().ne("").sum())
    if populated_count > MAX_BATCH_JOBS:
        raise ValueError(
            f"The CSV contains {populated_count} jobs. The public app accepts "
            f"a maximum of {MAX_BATCH_JOBS} jobs per run."
        )

    return frame


def read_uploaded_csv(uploaded_file: Any) -> pd.DataFrame:
    """Read and validate a user-uploaded UTF-8 CSV file."""

    try:
        uploaded_file.seek(0)
        frame = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)
    except UnicodeDecodeError as exc:
        raise ValueError("Save the CSV as UTF-8 and upload it again.") from exc
    except pd.errors.EmptyDataError as exc:
        raise ValueError("The uploaded CSV is empty.") from exc
    except pd.errors.ParserError as exc:
        raise ValueError(f"The CSV could not be parsed: {exc}") from exc

    return normalise_csv_columns(frame)


def reset_batch_editor() -> None:
    st.session_state.batch_rows = pd.DataFrame([EMPTY_BATCH_ROW])
    st.session_state.batch_editor_version = (
        st.session_state.get("batch_editor_version", 0) + 1
    )


if "batch_rows" not in st.session_state:
    reset_batch_editor()

st.title("🎬 Video & Audio Downloader")
st.caption("Prepare one download or a batch of independent download jobs.")

st.warning(
    "Use this tool only for your own uploads, public-domain material, "
    "or content whose owner has authorised downloading. The app does not "
    "bypass private access, authentication, or DRM."
)

mode = st.radio(
    "Job setup",
    options=["quick", "batch"],
    format_func=lambda value: (
        "Quick batch - one setting for every URL"
        if value == "quick"
        else "Advanced batch - import CSV and/or enter jobs manually"
    ),
)

job_rows: list[dict[str, str]] = []
submitted = False
permission_confirmed = False

if mode == "quick":
    with st.form("quick_batch_form"):
        raw_urls = st.text_area(
            "Video URL(s)",
            placeholder=(
                "Paste up to ten URLs, separated by commas, spaces, or new lines"
            ),
            height=150,
        )

        download_type = st.selectbox(
            "Download type",
            options=list(DOWNLOAD_LABELS),
            format_func=DOWNLOAD_LABELS.get,
        )

        scope = st.radio(
            "Download scope",
            options=["full", "segment"],
            format_func=lambda value: (
                "Full video" if value == "full" else "Specific segment"
            ),
            horizontal=True,
            disabled=download_type == "thumbnail",
        )

        col1, col2 = st.columns(2)
        with col1:
            start_text = st.text_input(
                "Start timestamp",
                placeholder="00:00:10",
                disabled=scope != "segment" or download_type == "thumbnail",
            )
        with col2:
            end_text = st.text_input(
                "End timestamp",
                placeholder="00:00:30",
                disabled=scope != "segment" or download_type == "thumbnail",
            )

        permission_confirmed = st.checkbox(
            "I own this content or have permission to download and use it."
        )
        submitted = st.form_submit_button("Prepare batch", type="primary")

    if submitted:
        try:
            urls = parse_urls(raw_urls)
            job_rows = [
                {
                    "url": url,
                    "download_type": download_type,
                    "scope": "full" if download_type == "thumbnail" else scope,
                    "start": start_text,
                    "end": end_text,
                }
                for url in urls
            ]
        except ValueError as exc:
            st.error(str(exc))
            submitted = False

else:
    st.info(
        "Import a CSV template, enter jobs directly in the table, or combine "
        "both methods. Each populated row is processed as an independent job."
    )

    st.download_button(
        "Download CSV template",
        data=CSV_TEMPLATE.encode("utf-8"),
        file_name="youtube_download_batch_template.csv",
        mime="text/csv",
    )

    uploaded_csv = st.file_uploader(
        "Import batch CSV (optional)",
        type=["csv"],
        help=(
            "Accepted columns: url, download_type, scope, start, end. "
            "The imported rows remain editable before processing."
        ),
    )

    import_col, clear_col = st.columns(2)
    with import_col:
        import_csv = st.button(
            "Load CSV into table",
            disabled=uploaded_csv is None,
            use_container_width=True,
        )
    with clear_col:
        clear_rows = st.button("Clear table", use_container_width=True)

    if clear_rows:
        reset_batch_editor()
        st.rerun()

    if import_csv and uploaded_csv is not None:
        try:
            imported_frame = read_uploaded_csv(uploaded_csv)
            existing_frame = st.session_state.batch_rows.copy()
            existing_non_blank = existing_frame[
                existing_frame["url"].astype(str).str.strip().ne("")
            ]

            if existing_non_blank.empty:
                combined_frame = imported_frame
            else:
                combined_frame = pd.concat(
                    [existing_non_blank, imported_frame],
                    ignore_index=True,
                )

            populated_count = int(
                combined_frame["url"].astype(str).str.strip().ne("").sum()
            )
            if populated_count > MAX_BATCH_JOBS:
                raise ValueError(
                    f"The combined manual and CSV input contains {populated_count} "
                    f"jobs. The maximum is {MAX_BATCH_JOBS}."
                )

            st.session_state.batch_rows = combined_frame
            st.session_state.batch_editor_version += 1
            st.success(
                f"Loaded {len(imported_frame)} CSV row(s). Review or edit them below."
            )
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    edited_rows = st.data_editor(
        st.session_state.batch_rows,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_order=BATCH_COLUMNS,
        column_config={
            "url": st.column_config.TextColumn(
                "Video URL",
                help="One permitted media URL per row.",
                width="large",
            ),
            "download_type": st.column_config.SelectboxColumn(
                "Type",
                options=list(DOWNLOAD_LABELS),
                required=True,
            ),
            "scope": st.column_config.SelectboxColumn(
                "Scope",
                options=["full", "segment"],
                required=True,
            ),
            "start": st.column_config.TextColumn(
                "Start",
                help="Required only for segment jobs, e.g. 00:00:10.",
            ),
            "end": st.column_config.TextColumn(
                "End",
                help="Required only for segment jobs, e.g. 00:00:30.",
            ),
        },
        key=f"batch_job_editor_{st.session_state.batch_editor_version}",
    )
    st.session_state.batch_rows = edited_rows

    populated_jobs = int(
        edited_rows["url"].astype(str).str.strip().ne("").sum()
    )
    st.caption(
        f"{populated_jobs}/{MAX_BATCH_JOBS} jobs entered. Thumbnail rows are "
        "always treated as full scope. Blank rows are ignored."
    )

    with st.expander("CSV format and accepted values"):
        st.code(CSV_TEMPLATE, language="csv")
        st.markdown(
            "**download_type:** `video`, `silent`, `audio`, or `thumbnail`  \n"
            "**scope:** `full` or `segment`  \n"
            "**timestamps:** `SS`, `MM:SS`, or `HH:MM:SS`"
        )

    permission_confirmed = st.checkbox(
        "I own this content or have permission to download and use it.",
        key="batch_permission",
    )
    submitted = st.button("Prepare batch", type="primary")

    if submitted:
        job_rows = edited_rows.fillna("").to_dict(orient="records")

if submitted:
    if not permission_confirmed:
        st.error("Confirm that you have permission before continuing.")
    else:
        status_box = st.empty()
        messages: list[str] = []

        def update_status(message: str) -> None:
            messages.append(message)
            status_box.info("\n\n".join(messages[-8:]))

        session_dir: Path | None = None
        try:
            with st.spinner("Running batch jobs and preparing one ZIP file..."):
                archive_path, results, session_dir = create_batch_download_archive(
                    job_rows=job_rows,
                    log_callback=update_status,
                )

            successful = sum(bool(result["success"]) for result in results)
            failed = len(results) - successful
            st.success(
                f"Batch completed. Successful: {successful}. Failed: {failed}."
            )

            summary_rows = [
                {
                    "Job": result["job_number"],
                    "Type": result["download_type"],
                    "Status": "Completed" if result["success"] else "Failed",
                    "URL": result["url"],
                }
                for result in results
            ]
            st.dataframe(summary_rows, hide_index=True, width="stretch")

            archive_bytes = archive_path.read_bytes()
            st.download_button(
                "Download batch ZIP",
                data=archive_bytes,
                file_name="downloaded_media_batch.zip",
                mime="application/zip",
                type="primary",
            )

            if failed:
                with st.expander("View failed jobs"):
                    for result in results:
                        if not result["success"]:
                            st.error(
                                f"Job {result['job_number']} ({result['url']}): "
                                f"{result['error']}"
                            )
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"Unexpected error: {exc}")
        finally:
            if session_dir is not None:
                cleanup_session(session_dir)

st.divider()
st.caption(
    "Free hosting has limited CPU, memory, temporary storage, and network "
    "capacity. Use short media and small batches for reliable results."
)
