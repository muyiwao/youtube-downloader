"""Streamlit interface for permitted single and batch media downloads."""

from __future__ import annotations

from pathlib import Path

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
        else "Advanced batch - configure each URL separately"
    ),
)

job_rows: list[dict[str, str]] = []

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
        "Add one row per job. The same URL may appear more than once when you "
        "need different segments or output types. Blank rows are ignored."
    )

    default_rows = pd.DataFrame(
        [
            {
                "url": "",
                "download_type": "video",
                "scope": "full",
                "start": "",
                "end": "",
            }
        ]
    )

    edited_rows = st.data_editor(
        default_rows,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
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
        key="batch_job_editor",
    )

    st.caption(
        f"Maximum {MAX_BATCH_JOBS} independent jobs per run. Thumbnail rows "
        "are always treated as full scope."
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
