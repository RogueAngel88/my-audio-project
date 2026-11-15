import io
import re
import unicodedata
import zipfile
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

API_URL = "https://api.jamendo.com/v3.0/tracks/"

st.set_page_config(page_title="My Audio Project", page_icon="🎵", layout="wide")
st.title("🎵 My Audio Project")
st.write("Search Jamendo for tracks that match your playlist CSV files.")

st.info(
    "This app searches Jamendo's independent-music catalog. It does not convert "
    "Spotify tracks or bypass Spotify protections. Most mainstream commercial "
    "recordings may not be available."
)

def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()

def normalize(value):
    """Normalize case, accents and punctuation, but do not remove meaningful words."""
    value = unicodedata.normalize("NFKD", clean_text(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())

def find_column(columns, choices):
    lookup = {str(c).strip().lower(): c for c in columns}
    for choice in choices:
        if choice in lookup:
            return lookup[choice]
    return None

def read_playlist_csv(file_bytes, filename):
    try:
        df = pd.read_csv(io.BytesIO(file_bytes))
    except Exception as exc:
        return None, f"{filename}: couldn't read CSV ({exc})."

    track_col = find_column(df.columns, ["track name", "song title", "track", "title", "name"])
    artist_col = find_column(df.columns, ["artist name", "artist", "artists"])
    if not track_col or not artist_col:
        return None, f"{filename}: couldn't identify both song-title and artist columns."

    out = pd.DataFrame({
        "Playlist": Path(filename).stem,
        "Track": df[track_col].map(clean_text),
        "Artist": df[artist_col].map(clean_text),
    })
    out = out[(out["Track"] != "") & (out["Artist"] != "")]
    return out, None

def get_uploaded_csvs(uploaded):
    files = []
    for item in uploaded:
        raw = item.getvalue()
        if item.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    for name in zf.namelist():
                        if name.lower().endswith(".csv") and not name.startswith("__MACOSX/"):
                            files.append((Path(name).name, zf.read(name)))
            except zipfile.BadZipFile:
                st.error(f"{item.name} is not a valid ZIP file.")
        elif item.name.lower().endswith(".csv"):
            files.append((item.name, raw))
    return files

def jamendo_search(client_id, track, artist):
    # Search by both requested fields, then independently verify exact normalized values.
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": 50,
        "search": track,
        "artist_name": artist,
        "include": "licenses",
        "audiodlformat": "mp32",
    }
    response = requests.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    headers = payload.get("headers", {})
    if headers.get("status") != "success":
        raise RuntimeError(headers.get("error_message", "Jamendo API error"))
    return payload.get("results", [])

client_id = st.text_input(
    "Jamendo API client ID",
    type="password",
    help="Enter your Jamendo developer Client ID. Do not put credentials in public source code."
)

uploads = st.file_uploader(
    "Upload your separated playlist CSV files or the ZIP containing them",
    type=["csv", "zip"],
    accept_multiple_files=True
)

if uploads:
    sources = get_uploaded_csvs(uploads)
    frames, errors = [], []
    for filename, raw in sources:
        frame, error = read_playlist_csv(raw, filename)
        if error:
            errors.append(error)
        elif frame is not None and not frame.empty:
            frames.append(frame)

    for error in errors:
        st.warning(error)

    if frames:
        library = pd.concat(frames, ignore_index=True).drop_duplicates()
        st.success(
            f"Loaded {len(library)} tracks from "
            f"{library['Playlist'].nunique()} playlist file(s)."
        )
        st.dataframe(library.head(25), use_container_width=True, hide_index=True)

        playlist_names = ["All playlists"] + sorted(library["Playlist"].unique().tolist())
        chosen = st.selectbox("Choose a playlist", playlist_names)
        view = library if chosen == "All playlists" else library[library["Playlist"] == chosen]

        max_tracks = min(100, len(view))
        count = st.number_input(
            "Maximum tracks to search in this run",
            min_value=1,
            max_value=max(1, max_tracks),
            value=min(10, max_tracks),
            step=1
        )

        st.caption(
            "Strict matching is enabled: a result is accepted only when both the "
            "normalized song title and artist match. Similar titles, covers, remixes "
            "and unrelated artists are rejected."
        )

        if st.button(
            "Search Jamendo for exact downloadable matches",
            type="primary",
            disabled=not client_id
        ):
            rows = []
            progress = st.progress(0)
            status = st.empty()
            selected = view.head(int(count)).reset_index(drop=True)

            for i, row in selected.iterrows():
                status.write(
                    f"Checking {i+1}/{len(selected)}: "
                    f"{row['Track']} - {row['Artist']}"
                )
                try:
                    candidates = jamendo_search(client_id, row["Track"], row["Artist"])
                    exact = []
                    for candidate in candidates:
                        title_ok = normalize(candidate.get("name", "")) == normalize(row["Track"])
                        artist_ok = normalize(candidate.get("artist_name", "")) == normalize(row["Artist"])
                        if title_ok and artist_ok:
                            exact.append(candidate)

                    downloadable = [
                        c for c in exact
                        if c.get("audiodownload_allowed") is True and c.get("audiodownload")
                    ]

                    if downloadable:
                        for candidate in downloadable:
                            rows.append({
                                "Playlist": row["Playlist"],
                                "Requested track": row["Track"],
                                "Requested artist": row["Artist"],
                                "Matched track": candidate.get("name", ""),
                                "Matched artist": candidate.get("artist_name", ""),
                                "Match status": "Exact title + artist",
                                "Download allowed": "Yes",
                                "License": candidate.get("license_ccurl", ""),
                                "Download URL": candidate.get("audiodownload", ""),
                                "Jamendo page": f"https://www.jamendo.com/track/{candidate.get('id', '')}",
                            })
                    elif exact:
                        for candidate in exact:
                            rows.append({
                                "Playlist": row["Playlist"],
                                "Requested track": row["Track"],
                                "Requested artist": row["Artist"],
                                "Matched track": candidate.get("name", ""),
                                "Matched artist": candidate.get("artist_name", ""),
                                "Match status": "Exact title + artist, but download not permitted",
                                "Download allowed": "No",
                                "License": candidate.get("license_ccurl", ""),
                                "Download URL": "",
                                "Jamendo page": f"https://www.jamendo.com/track/{candidate.get('id', '')}",
                            })
                    else:
                        rows.append({
                            "Playlist": row["Playlist"],
                            "Requested track": row["Track"],
                            "Requested artist": row["Artist"],
                            "Matched track": "",
                            "Matched artist": "",
                            "Match status": "No exact match found",
                            "Download allowed": "No",
                            "License": "",
                            "Download URL": "",
                            "Jamendo page": "",
                        })
                except Exception as exc:
                    rows.append({
                        "Playlist": row["Playlist"],
                        "Requested track": row["Track"],
                        "Requested artist": row["Artist"],
                        "Matched track": "",
                        "Matched artist": "",
                        "Match status": f"Search error: {exc}",
                        "Download allowed": "No",
                        "License": "",
                        "Download URL": "",
                        "Jamendo page": "",
                    })
                progress.progress((i + 1) / len(selected))

            status.success(
                "Search finished. Only exact title-and-artist matches are listed as matches. "
                "Check the license and track page before downloading."
            )
            st.session_state["results_df"] = pd.DataFrame(rows)

if "results_df" in st.session_state:
    results_df = st.session_state["results_df"]
    st.subheader("Search results")

    exact_count = int((results_df["Match status"] == "Exact title + artist").sum())
    no_match_count = int((results_df["Match status"] == "No exact match found").sum())
    c1, c2 = st.columns(2)
    c1.metric("Exact downloadable matches", exact_count)
    c2.metric("Tracks without an exact match", no_match_count)

    st.dataframe(
        results_df.drop(columns=["Download URL"]),
        use_container_width=True,
        hide_index=True
    )

    st.download_button(
        "Download search results as CSV",
        results_df.drop(columns=["Download URL"]).to_csv(index=False).encode("utf-8-sig"),
        file_name="jamendo_exact_match_results.csv",
        mime="text/csv"
    )

    st.subheader("Verified downloadable tracks")
    downloadable_rows = results_df[
        (results_df["Match status"] == "Exact title + artist") &
        (results_df["Download allowed"] == "Yes")
    ]

    if downloadable_rows.empty:
        st.info(
            "No exact downloadable matches were found in this search. "
            "That is better than offering unrelated songs as substitutes."
        )
    else:
        for idx, item in downloadable_rows.iterrows():
            title = f"{item['Matched track']} - {item['Matched artist']}"
            with st.expander(title):
                st.write(f"Playlist: {item['Playlist']}")
                st.write(f"License: {item['License'] or 'Open the track page to verify the license.'}")
                st.markdown(f"[Open Jamendo track page]({item['Jamendo page']})")
                st.warning(
                    "Confirm that this is the correct recording and review the license "
                    "terms before downloading."
                )
                try:
                    audio = requests.get(item["Download URL"], timeout=60)
                    audio.raise_for_status()
                    safe_name = re.sub(
                        r"[^A-Za-z0-9._ -]+", "",
                        f"{item['Matched artist']} - {item['Matched track']}"
                    ).strip()
                    st.download_button(
                        "Download MP3",
                        data=audio.content,
                        file_name=f"{safe_name or 'jamendo-track'}.mp3",
                        mime="audio/mpeg",
                        key=f"download_{idx}"
                    )
                except Exception as exc:
                    st.warning(f"Could not retrieve this file right now: {exc}")
