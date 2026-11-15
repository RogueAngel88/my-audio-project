import io
import re
import zipfile
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

API_URL = "https://api.jamendo.com/v3.0/tracks/"

st.set_page_config(page_title="My Audio Project", page_icon="🎵", layout="wide")
st.title("🎵 My Audio Project")
st.write("Find downloadable, openly licensed music that matches your playlist CSV files.")
st.info(
    "This app searches Jamendo's independent-music catalog. It does not convert Spotify "
    "tracks or bypass Spotify protections. Mainstream commercial songs may not be available."
)

def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()

def find_column(columns, choices):
    lookup = {str(c).strip().lower(): c for c in columns}
    for choice in choices:
        if choice in lookup:
            return lookup[choice]
    return None

def read_playlist_csv(file_bytes, filename):
    df = pd.read_csv(io.BytesIO(file_bytes))
    track_col = find_column(df.columns, [
        "track name", "song title", "track", "title", "name"
    ])
    artist_col = find_column(df.columns, [
        "artist name", "artist", "artists"
    ])
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
    query = f"{track} {artist}"
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": 5,
        "search": query,
        "include": "licenses",
        "audiodlformat": "mp32",
    }
    response = requests.get(API_URL, params=params, timeout=25)
    response.raise_for_status()
    payload = response.json()
    if payload.get("headers", {}).get("status") != "success":
        raise RuntimeError(payload.get("headers", {}).get("error_message", "Jamendo API error"))
    return payload.get("results", [])

client_id = st.text_input(
    "Jamendo API client ID",
    type="password",
    help="Create a free developer account at developer.jamendo.com and register an app to get your own client ID."
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
        st.success(f"Loaded {len(library)} tracks from {library['Playlist'].nunique()} playlist file(s).")
        st.dataframe(library.head(30), use_container_width=True, hide_index=True)

        playlist_names = ["All playlists"] + sorted(library["Playlist"].unique().tolist())
        chosen = st.selectbox("Choose a playlist", playlist_names)
        view = library if chosen == "All playlists" else library[library["Playlist"] == chosen]
        max_tracks = min(100, len(view))
        count = st.number_input("Maximum tracks to search in this run", min_value=1, max_value=max(1, max_tracks), value=min(20, max_tracks))
        if st.button("Search Jamendo for downloadable matches", type="primary", disabled=not client_id):
            rows = []
            progress = st.progress(0)
            status = st.empty()
            selected = view.head(int(count)).reset_index(drop=True)
            for i, row in selected.iterrows():
                status.write(f"Searching {i+1}/{len(selected)}: {row['Track']} - {row['Artist']}")
                try:
                    results = jamendo_search(client_id, row["Track"], row["Artist"])
                    allowed = [x for x in results if x.get("audiodownload_allowed") and x.get("audiodownload")]
                    if allowed:
                        for candidate in allowed:
                            rows.append({
                                "Playlist": row["Playlist"],
                                "Requested track": row["Track"],
                                "Requested artist": row["Artist"],
                                "Jamendo track": candidate.get("name", ""),
                                "Jamendo artist": candidate.get("artist_name", ""),
                                "License": candidate.get("license_ccurl", ""),
                                "Download allowed": True,
                                "Download URL": candidate.get("audiodownload", ""),
                                "Jamendo page": f"https://www.jamendo.com/track/{candidate.get('id','')}",
                            })
                    else:
                        rows.append({
                            "Playlist": row["Playlist"],
                            "Requested track": row["Track"],
                            "Requested artist": row["Artist"],
                            "Jamendo track": "",
                            "Jamendo artist": "",
                            "License": "",
                            "Download allowed": False,
                            "Download URL": "",
                            "Jamendo page": "",
                        })
                except Exception as exc:
                    rows.append({
                        "Playlist": row["Playlist"],
                        "Requested track": row["Track"],
                        "Requested artist": row["Artist"],
                        "Jamendo track": "Search error",
                        "Jamendo artist": str(exc),
                        "License": "",
                        "Download allowed": False,
                        "Download URL": "",
                        "Jamendo page": "",
                    })
                progress.progress((i + 1) / len(selected))
            status.success("Search finished. Check every match before downloading; similar titles may be different recordings.")
            results_df = pd.DataFrame(rows)
            st.session_state["results_df"] = results_df

if "results_df" in st.session_state:
    results_df = st.session_state["results_df"]
    st.subheader("Search results")
    display = results_df.drop(columns=["Download URL"])
    st.dataframe(display, use_container_width=True, hide_index=True)
    st.download_button(
        "Download search results as CSV",
        results_df.drop(columns=["Download URL"]).to_csv(index=False).encode("utf-8-sig"),
        file_name="jamendo_search_results.csv",
        mime="text/csv"
    )
    st.caption("Download links are shown below only for tracks Jamendo marks as downloadable.")
    for idx, item in results_df.iterrows():
        if item.get("Download allowed") and item.get("Download URL"):
            label = f"{item['Jamendo track']} - {item['Jamendo artist']} (requested: {item['Requested track']} - {item['Requested artist']})"
            with st.expander(label):
                st.write(f"Playlist: {item['Playlist']}")
                st.write(f"License: {item['License'] or 'Check the Jamendo track page for license details.'}")
                st.markdown(f"[Open Jamendo track page]({item['Jamendo page']})")
                try:
                    audio = requests.get(item["Download URL"], timeout=45)
                    audio.raise_for_status()
                    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "", f"{item['Jamendo artist']} - {item['Jamendo track']}").strip()
                    st.download_button(
                        "Download MP3",
                        data=audio.content,
                        file_name=f"{safe_name or 'jamendo-track'}.mp3",
                        mime="audio/mpeg",
                        key=f"download_{idx}"
                    )
                except Exception as exc:
                    st.warning(f"Could not retrieve this file right now: {exc}")
