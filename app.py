import io
import re
import zipfile
import unicodedata
from urllib.parse import quote
from pathlib import Path
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Free Music Finder", page_icon="🎵", layout="wide")
st.title("🎵 Free Music Finder")
st.caption("Find exact matches from free-download catalogs. No Spotify audio is extracted.")

JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"
IA_SEARCH = "https://archive.org/advancedsearch.php"

def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

def find_col(df, choices):
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for choice in choices:
        if choice in lookup:
            return lookup[choice]
    return None

def read_playlists(uploads):
    playlists = []
    for upload in uploads:
        archive_name = Path(upload.name).stem
        with zipfile.ZipFile(upload) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv") or name.startswith("__MACOSX/"):
                    continue
                try:
                    raw = zf.read(name)
                    df = pd.read_csv(io.BytesIO(raw))
                except Exception:
                    continue
                title_col = find_col(df, ["track name", "song title", "title", "track"])
                artist_col = find_col(df, ["artist name", "artist", "artists"])
                if title_col is None or artist_col is None:
                    continue
                for _, row in df.iterrows():
                    title = str(row.get(title_col, "")).strip()
                    artist = str(row.get(artist_col, "")).strip()
                    if title and artist and title.lower() != "nan" and artist.lower() != "nan":
                        playlists.append({
                            "Playlist": f"{archive_name} / {Path(name).stem}",
                            "Requested track": title,
                            "Requested artist": artist
                        })
    # Remove repeated CSV rows within the same playlist, keeping first occurrence.
    seen, unique = set(), []
    for item in playlists:
        key = (norm(item["Playlist"]), norm(item["Requested track"]), norm(item["Requested artist"]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return pd.DataFrame(unique)

def jamendo_search(title, artist, client_id):
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": 50,
        "search": title,
        "audioformat": "mp32",
        "include": "licenses",
    }
    try:
        response = requests.get(JAMENDO_API, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return []
    matches, seen = [], set()
    for track in data.get("results", []):
        track_title = track.get("name", "")
        track_artist = track.get("artist_name", "")
        # Exact normalized title and artist required. No fuzzy or title-only matches.
        if norm(track_title) != norm(title) or norm(track_artist) != norm(artist):
            continue
        if not track.get("audiodownload_allowed") or not track.get("audiodownload"):
            continue
        track_id = str(track.get("id", ""))
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        matches.append({
            "Source": "Jamendo",
            "Matched title": track_title,
            "Matched artist": track_artist,
            "License": track.get("license_ccurl") or "See Jamendo track page",
            "Source page": track.get("shareurl") or "",
            "Download URL": track.get("audiodownload"),
            "Status": "Jamendo marks audio download as allowed"
        })
    return matches

def archive_discovery(title, artist):
    # Internet Archive is discovery-only. Its uploader-supplied metadata/license
    # cannot independently prove rights, so no IA item is ever download-eligible.
    query = f'mediatype:audio AND title:"{title}" AND creator:"{artist}"'
    params = {"q": query, "fl[]": ["identifier", "title", "creator"], "rows": 20, "output": "json"}
    try:
        response = requests.get(IA_SEARCH, params=params, timeout=25)
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
    except Exception:
        return []
    found, seen = [], set()
    for doc in docs:
        identifier = str(doc.get("identifier", ""))
        if not identifier or identifier in seen:
            continue
        title_value = doc.get("title", "")
        creator_value = doc.get("creator", "")
        if isinstance(title_value, list):
            title_value = title_value[0] if title_value else ""
        if isinstance(creator_value, list):
            creator_value = creator_value[0] if creator_value else ""
        if norm(title_value) != norm(title) or norm(creator_value) != norm(artist):
            continue
        seen.add(identifier)
        found.append({
            "Source": "Internet Archive (unverified)",
            "Matched title": title_value,
            "Matched artist": creator_value,
            "License": "Uploader metadata only; not independently verified",
            "Source page": f"https://archive.org/details/{quote(identifier)}",
            "Download URL": "",
            "Status": "Discovery only. Not eligible for download."
        })
    return found

with st.sidebar:
    st.header("Settings")
    client_id = st.text_input("Jamendo Client ID", type="password", help="Enter your own Jamendo Client ID. It is not stored in this app.")
    include_ia = st.checkbox("Show Internet Archive discovery results (not downloadable)", value=False)
    max_tracks = st.selectbox("Tracks to test", [10, 25, 50, 100, "All"], index=0)

uploads = st.file_uploader("Upload your Spotify playlist ZIP files", type=["zip"], accept_multiple_files=True)
if uploads:
    try:
        tracks_df = read_playlists(uploads)
    except Exception as exc:
        st.error(f"Could not read ZIP: {exc}")
        tracks_df = pd.DataFrame()
    if tracks_df.empty:
        st.error("No usable CSV tracks found. CSVs need columns such as Track name and Artist name.")
    else:
        st.success(f"Loaded {len(tracks_df):,} unique tracks from {tracks_df['Playlist'].nunique()} playlists across {len(uploads)} ZIP file(s).")
        playlist_names = sorted(tracks_df["Playlist"].unique().tolist())
        selected = st.multiselect("Choose playlist(s)", playlist_names, default=playlist_names[:1])
        subset = tracks_df[tracks_df["Playlist"].isin(selected)].copy()
        if max_tracks != "All":
            subset = subset.head(int(max_tracks))
        st.write(f"Tracks queued for search: **{len(subset)}**")
        if st.button("Search selected free sources", type="primary", disabled=not client_id.strip()):
            if not client_id.strip():
                st.warning("Enter your Jamendo Client ID in the sidebar.")
            else:
                rows, eligible, discovery = [], [], []
                progress = st.progress(0)
                status = st.empty()
                total = max(len(subset), 1)
                for index, (_, item) in enumerate(subset.iterrows(), start=1):
                    title, artist = item["Requested track"], item["Requested artist"]
                    jamendo = jamendo_search(title, artist, client_id.strip())
                    ia = archive_discovery(title, artist) if include_ia else []
                    if jamendo:
                        for match in jamendo:
                            record = {**item.to_dict(), **match, "Download allowed": "Yes"}
                            eligible.append(record)
                        rows.append({**item.to_dict(), "Source": "Jamendo", "Match status": f"{len(jamendo)} exact downloadable match(es)", "Download allowed": "Yes"})
                    elif ia:
                        for match in ia:
                            discovery.append({**item.to_dict(), **match, "Download allowed": "No"})
                        rows.append({**item.to_dict(), "Source": "Internet Archive", "Match status": "Unverified discovery result only", "Download allowed": "No"})
                    else:
                        rows.append({**item.to_dict(), "Source": "Jamendo" + (" + Internet Archive" if include_ia else ""), "Match status": "No eligible exact free match found", "Download allowed": "No"})
                    progress.progress(index / total)
                    status.caption(f"Checked {index} of {total}: {title} - {artist}")
                st.session_state["search_rows"] = rows
                st.session_state["eligible_rows"] = eligible
                st.session_state["discovery_rows"] = discovery
                st.session_state["search_key"] = (tuple(selected), len(subset))
                status.success("Search finished. Jamendo download permissions are shown as reported by Jamendo. Review source pages and license terms.")
                progress.empty()

if "search_rows" in st.session_state:
    st.subheader("Search results")
    results = pd.DataFrame(st.session_state["search_rows"])
    st.metric("Tracks checked", len(results))
    st.dataframe(results, use_container_width=True, hide_index=True)
    st.download_button("Download results CSV", results.to_csv(index=False).encode("utf-8-sig"), "music_search_results.csv", "text/csv")
    eligible = st.session_state.get("eligible_rows", [])
    st.subheader("Jamendo exact-match downloads")
    if eligible:
        for i, item in enumerate(eligible):
            with st.expander(f"{item['Matched title']} - {item['Matched artist']} (Jamendo)"):
                st.write(f"Playlist: {item['Playlist']}")
                st.write(f"License: {item['License']}")
                if item["Source page"]:
                    st.markdown(f"[Open Jamendo source page]({item['Source page']})")
                st.warning("Jamendo reports that downloads are allowed for this track. Review the track page and license before use.")
                st.link_button("Open Jamendo download", item["Download URL"])
    else:
        st.info("No exact Jamendo matches with download permission were found.")
    discovery = st.session_state.get("discovery_rows", [])
    if discovery:
        st.subheader("Internet Archive discovery only (not eligible for download)")
        st.warning("These are unverified uploader listings. No MP3 download links are provided.")
        df_discovery = pd.DataFrame(discovery)
        st.dataframe(df_discovery.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        for item in discovery:
            st.markdown(f"- [{item['Matched title']} - {item['Matched artist']}]({item['Source page']})")

st.caption("Important: exact metadata matches do not establish copyright ownership. Internet Archive listings are never treated as authorized downloads by this app. Jamendo download eligibility is based on Jamendo's API permission flag and should still be checked on the source page.")
