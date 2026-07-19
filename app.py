"""Streamlit user interface for the permitted-content media downloader."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from downloader import cleanup_session, create_download_archive

st.set_page_config(
    page_title="Video & Audio Downloader",
    page_icon="🎬",
    layout="centered",
)

st.title("🎬 Video & Audio Downloader")
st.caption("Download media you own or have permission to use.")

st.warning(
    "Use this tool only for your own uploads, public-domain material, "
    "or content whose owner has authorised downloading. The app does not "
    "bypass private access, authentication, or DRM."
)

with st.form("download_form"):
    raw_urls = st.text_area(
        "Video URL(s)",
        placeholder="Paste up to five URLs, separated by spaces, commas, or new lines",
        height=130,
    )

    download_type = st.selectbox(
        "Download type",
        options=["video", "silent", "audio", "thumbnail"],
        format_func=lambda value: {
            "video": "Video with audio - MP4",
            "silent": "Video only - silent MP4",
            "audio": "Audio only - MP3",
            "thumbnail": "Thumbnail - JPG",
        }[value],
    )

    scope = st.radio(
        "Download scope",
        options=["full", "segment"],
        format_func=lambda value: "Full video" if value == "full" else "Specific segment",
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
    submitted = st.form_submit_button("Prepare download", type="primary")

if submitted:
    if not permission_confirmed:
        st.error("Confirm that you have permission before continuing.")
    else:
        status_box = st.empty()
        messages: list[str] = []

        def update_status(message: str) -> None:
            messages.append(message)
            status_box.info("\n\n".join(messages[-5:]))

        session_dir: Path | None = None
        try:
            with st.spinner("Downloading and preparing your ZIP file..."):
                archive_path, results, session_dir = create_download_archive(
                    raw_urls=raw_urls,
                    download_type=download_type,
                    scope="full" if download_type == "thumbnail" else scope,
                    start_text=start_text,
                    end_text=end_text,
                    log_callback=update_status,
                )

            successful = sum(result["success"] for result in results)
            failed = len(results) - successful
            st.success(f"Completed {successful} job(s). Failed: {failed}.")

            archive_bytes = archive_path.read_bytes()
            st.download_button(
                "Download ZIP",
                data=archive_bytes,
                file_name="downloaded_media.zip",
                mime="application/zip",
                type="primary",
            )

            if failed:
                with st.expander("View failed jobs"):
                    for result in results:
                        if not result["success"]:
                            st.error(
                                f"Job {result['job_number']}: {result['error']}"
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
    "Free hosting has limited CPU, memory, storage, and network access. "
    "Use short clips and small batches for the most reliable results."
)
