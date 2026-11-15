import io
import csv
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
CCMIXTER_API = "https://ccmixter.org/api/query"
OPENVERSE_API = "https://api.openverse.org/v1/audio/"

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




def openverse_discovery(title, artist):
    """Search Openverse for exact-title openly licensed audio candidates.

    Results are discovery-only. Openverse metadata and a listed license do not
    independently establish that the uploader owns the recording or that it is
    the requested original recording/authorized cover. No direct audio URLs are
    exposed as download buttons.
    """
    params = {
        "title": title,
        "category": "music",
        "extension": "mp3",
        "filter_dead": "true",
        "page_size": 20,
    }
    try:
        response = requests.get(
            OPENVERSE_API,
            params=params,
            timeout=25,
            headers={"User-Agent": "FreeMusicFinder/1.0"}
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
    except Exception:
        return []

    found, seen = [], set()
    for item in results:
        matched_title = str(item.get("title") or "").strip()
        creator = str(item.get("creator") or "").strip()
        if not matched_title or norm(matched_title) != norm(title):
            continue
        item_id = str(item.get("id") or item.get("foreign_landing_url") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)

        tags = item.get("tags") or []
        tag_text = " ".join(
            str(tag.get("name", "") if isinstance(tag, dict) else tag)
            for tag in tags
        ).lower()
        evidence = f"{matched_title} {tag_text}".lower()
        if norm(creator) == norm(artist):
            category = "Exact title + creator metadata (unverified)"
            match_type = "Creator metadata matches requested artist; recording identity still unverified"
        elif any(term in evidence for term in ("cover", "tribute", "remix", "karaoke", "instrumental")):
            category = "Possible cover/remix or alternate version (unverified)"
            match_type = "Title and tags suggest an alternate version; verify recording and rights"
        else:
            category = "Same title only (not a confirmed alternative)"
            match_type = "Creator differs or is missing; no reliable relationship to requested artist established"

        license_name = str(item.get("license") or "Not provided")
        license_version = str(item.get("license_version") or "")
        if license_version:
            license_name = f"{license_name} {license_version}"
        found.append({
            "Source": "Openverse (discovery only)",
            "Matched title": matched_title,
            "Creator": creator or "Not provided",
            "Category": category,
            "Match type": match_type,
            "License": license_name,
            "License URL": str(item.get("license_url") or ""),
            "Provider": str(item.get("provider") or item.get("source") or "Not provided"),
            "Source page": str(item.get("foreign_landing_url") or item.get("detail_url") or ""),
            "Download URL": "",
            "Download allowed": "No",
            "Status": "Discovery only. Review the source page, license, attribution, and recording identity."
        })
    return found


def ccmixter_discovery(title, artist):
    """
    Search ccMixter by title. Separate exact title+display-name matches from
    same-title records uploaded by someone else. All ccMixter results remain
    discovery-only until the individual page, recording identity, and license
    are manually reviewed.
    """
    params = {
        "f": "csv",
        "dataview": "default",
        "limit": 50,
        "search": title,
        "search_type": "match",
    }
    diagnostics = {"api_status": "not checked", "columns": [], "rows_received": 0, "error": ""}
    try:
        response = requests.get(
            CCMIXTER_API,
            params=params,
            timeout=25,
            headers={"User-Agent": "FreeMusicFinder/1.0"}
        )
        diagnostics["api_status"] = str(response.status_code)
        response.raise_for_status()
        text_data = response.content.decode("utf-8-sig", errors="replace").strip()
        if not text_data:
            diagnostics["error"] = "The API returned an empty response."
            return [], diagnostics
        rows = list(csv.DictReader(io.StringIO(text_data)))
        if not rows:
            diagnostics["error"] = "The response contained no CSV data rows."
            return [], diagnostics
        diagnostics["columns"] = list(rows[0].keys())
        diagnostics["rows_received"] = len(rows)
        sample_fields = (
            "upload_id", "upload_name", "user_name", "user_real_name",
            "artist_page_url", "license_name", "license_url", "file_page_url"
        )
        diagnostics["sample_rows"] = [
            {field: str(row.get(field, "") or "")[:300] for field in sample_fields}
            for row in rows[:3]
        ]
    except Exception as exc:
        diagnostics["error"] = f"{type(exc).__name__}: {exc}"
        return [], diagnostics

    found, seen = [], set()
    for row in rows:
        lower = {str(k).strip().lower(): v for k, v in row.items() if k}
        name = str(lower.get("upload_name") or "").strip()
        username = str(lower.get("user_name") or "").strip()
        display_name = str(lower.get("user_real_name") or username).strip()
        upload_id = str(lower.get("upload_id") or "").strip()
        license_name = str(lower.get("license_name") or "Not provided").strip()
        license_url = str(lower.get("license_url") or "").strip()
        item_url = str(lower.get("file_page_url") or "").strip()

        description = str(
            lower.get("upload_description_plain")
            or lower.get("upload_description_html")
            or ""
        ).strip()
        tags = str(lower.get("upload_tags") or "").strip()
        evidence_text = f"{description} {tags}".lower()

        # Only consider exact same-title records. A different uploader is NOT
        # automatically a cover/remix. Require explicit text connecting the
        # requested artist to a cover/remix/version before calling it possible.
        if not name or norm(name) != norm(title):
            continue

        same_artist_metadata = (
            norm(display_name) == norm(artist) or norm(username) == norm(artist)
        )
        artist_mentioned = norm(artist).lower() in evidence_text
        relationship_terms = (
            "cover", "remix of", "remix for", "version of",
            "tribute to", "cover of", "re-recording of", "re-recorded"
        )
        relationship_mentioned = any(term in evidence_text for term in relationship_terms)

        if same_artist_metadata:
            match_type = "Exact title + artist/uploader metadata match (unverified)"
            category = "Exact metadata match"
            classification_reason = "Uploader/display metadata matches the requested artist; recording identity still needs source-page verification."
        elif artist_mentioned and relationship_mentioned:
            match_type = "Possible cover/remix: source text mentions artist and relationship"
            category = "Possible cover/remix (unverified)"
            classification_reason = "Source description/tags mention the requested artist and a cover/remix relationship. Verify the actual audio and rights."
        else:
            match_type = "Unrelated or unverified same-title recording"
            category = "Same title only (not a confirmed alternative)"
            classification_reason = "No clear source-page text connecting this upload to the requested artist's song."
        identity = (upload_id or norm(name), norm(username), match_type)
        if identity in seen:
            continue
        seen.add(identity)

        found.append({
            "Source": "ccMixter (discovery only)",
            "Matched title": name,
            "Uploader username": username,
            "Uploader/display name": display_name,
            "Category": category,
            "Match type": match_type,
            "Classification reason": classification_reason,
            "License": license_name,
            "License URL": license_url,
            "Source page": item_url,
            "Download URL": "",
            "Download allowed": "No",
            "Status": "Unverified listing. Review the item page, recording identity, license, attribution, and restrictions."
        })

    diagnostics["same_title_records"] = len(found)
    diagnostics["exact_title_artist_matches"] = sum(
        1 for item in found if item["Category"] == "Exact metadata match"
    )
    diagnostics["possible_cover_remix_records"] = sum(
        1 for item in found if item["Category"] == "Possible cover/remix (unverified)"
    )
    diagnostics["same_title_only_records"] = sum(
        1 for item in found if item["Category"] == "Same title only (not a confirmed alternative)"
    )
    return found, diagnostics

with st.sidebar:
    st.header("Settings")
    client_id = st.text_input("Jamendo Client ID", type="password", help="Enter your own Jamendo Client ID. It is not stored in this app.")
    include_ia = st.checkbox("Show Internet Archive discovery results (not downloadable)", value=False)
    include_ccmixter = st.checkbox("Search ccMixter (license review required)", value=True)
    include_openverse = st.checkbox("Search Openverse (open-license discovery only)", value=True)
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
                rows, eligible, discovery, ccmixter_results, openverse_results = [], [], [], [], []
                progress = st.progress(0)
                status = st.empty()
                total = max(len(subset), 1)
                for index, (_, item) in enumerate(subset.iterrows(), start=1):
                    title, artist = item["Requested track"], item["Requested artist"]
                    jamendo = jamendo_search(title, artist, client_id.strip())
                    ia = archive_discovery(title, artist) if include_ia else []
                    cc, cc_diag = ccmixter_discovery(title, artist) if include_ccmixter else ([], {"api_status": "disabled", "columns": [], "rows_received": 0, "error": ""})
                    ov = openverse_discovery(title, artist) if include_openverse else []
                    for match in ov:
                        openverse_results.append({**item.to_dict(), **match})
                    for match in cc:
                        ccmixter_results.append({**item.to_dict(), **match, "Download allowed": "No"})
                    if jamendo:
                        for match in jamendo:
                            record = {**item.to_dict(), **match, "Download allowed": "Yes"}
                            eligible.append(record)
                        rows.append({**item.to_dict(), "Source": "Jamendo", "Match status": f"{len(jamendo)} exact downloadable match(es)", "Download allowed": "Yes"})
                    elif ia:
                        # Collapse multiple Archive uploads for the same requested
                        # title/artist into one discovery row. Keep one source page
                        # and report how many distinct archive records matched.
                        unique_archive = {}
                        for match in ia:
                            key = (norm(match.get("Matched title", "")), norm(match.get("Matched artist", "")))
                            if key not in unique_archive:
                                unique_archive[key] = match
                        for match in unique_archive.values():
                            record = {**item.to_dict(), **match, "Download allowed": "No"}
                            record["Additional archive records"] = max(len(ia) - 1, 0)
                            discovery.append(record)
                        rows.append({**item.to_dict(), "Source": "Internet Archive", "Match status": "Unverified discovery result only", "Download allowed": "No"})
                    elif cc:
                        rows.append({**item.to_dict(), "Source": "ccMixter", "Match status": "Unverified ccMixter discovery result only", "Download allowed": "No"})
                    else:
                        source_labels = ["Jamendo"]
                        if include_ia:
                            source_labels.append("Internet Archive")
                        if include_ccmixter:
                            source_labels.append("ccMixter")
                        if include_openverse:
                            source_labels.append("Openverse")
                        rows.append({**item.to_dict(), "Source": " + ".join(source_labels), "Match status": "No eligible exact free match found", "Download allowed": "No"})
                    progress.progress(index / total)
                    status.caption(f"Checked {index} of {total}: {title} - {artist}")
                st.session_state["search_rows"] = rows
                st.session_state["eligible_rows"] = eligible
                st.session_state["discovery_rows"] = discovery
                st.session_state["ccmixter_rows"] = ccmixter_results
                st.session_state["openverse_rows"] = openverse_results
                st.session_state["ccmixter_diagnostics"] = cc_diag
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
        if not df_discovery.empty:
            df_discovery = df_discovery.drop_duplicates(
                subset=["Playlist", "Requested track", "Requested artist", "Matched title", "Matched artist"],
                keep="first"
            )
        st.dataframe(df_discovery.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        shown_links = set()
        for item in discovery:
            link_key = (norm(item["Requested track"]), norm(item["Requested artist"]))
            if link_key in shown_links:
                continue
            shown_links.add(link_key)
            st.markdown(f"- [{item['Requested track']} - {item['Requested artist']}]({item['Source page']})")
    openverse_rows = st.session_state.get("openverse_rows", [])
    st.subheader("Openverse openly licensed audio discovery (not pre-approved for download)")
    if openverse_rows:
        st.warning("Openverse indexes openly licensed media, but a license label does not prove the uploader owns the recording or that it is the requested original/authorized cover. No direct MP3 download links are provided. Open each source page and review the license and attribution requirements.")
        ov_df = pd.DataFrame(openverse_rows).drop_duplicates(
            subset=["Playlist", "Requested track", "Requested artist", "Matched title", "Creator", "Source page"],
            keep="first"
        )
        for category in [
            "Exact title + creator metadata (unverified)",
            "Possible cover/remix or alternate version (unverified)",
            "Same title only (not a confirmed alternative)",
        ]:
            st.markdown(f"**{category}**")
            subset_df = ov_df[ov_df["Category"] == category]
            if not subset_df.empty:
                st.dataframe(subset_df.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
                for item in subset_df.to_dict("records"):
                    if item.get("Source page"):
                        st.markdown(f"- [{item['Matched title']} - {item['Creator']}]({item['Source page']}) · {item['License']}")
            else:
                st.info("No results in this category.")
    else:
        st.info("No exact-title Openverse audio candidates were returned for this test.")

    cc_rows = st.session_state.get("ccmixter_rows", [])
    st.subheader("ccMixter discovery (license review required)")
    if cc_rows:
        st.warning("All ccMixter records are discovery-only. Same title alone does not establish that a recording is a cover or remix. Possible cover/remix labels require the source description/tags to mention both the requested artist and a relationship such as 'cover of' or 'remix of'. Verify the actual audio and license on the source page. No MP3 download links are provided.")
        cc_df = pd.DataFrame(cc_rows).drop_duplicates(
            subset=["Playlist", "Requested track", "Requested artist", "Matched title", "Uploader username", "Category"],
            keep="first"
        )
        exact_cc = cc_df[cc_df["Category"] == "Exact metadata match"]
        possible_cc = cc_df[cc_df["Category"] == "Possible cover/remix (unverified)"]
        same_title_cc = cc_df[cc_df["Category"] == "Same title only (not a confirmed alternative)"]

        st.markdown("**Exact title + artist/uploader metadata matches (still unverified)**")
        if not exact_cc.empty:
            st.dataframe(exact_cc.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        else:
            st.info("No exact title + artist/uploader metadata matches.")
        st.markdown("**Possible covers/remixes (source text provides a clue, not proof)**")
        if not possible_cc.empty:
            st.dataframe(possible_cc.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        else:
            st.info("No source descriptions/tags explicitly connected a same-title upload to the requested artist.")
        st.markdown("**Same title only (not a confirmed alternative)**")
        if not same_title_cc.empty:
            st.dataframe(same_title_cc.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        else:
            st.info("No unrelated same-title records were returned.")
        for item in cc_df.to_dict("records"):
            if item.get("Source page"):
                st.markdown(f"- [{item['Matched title']} - {item['Uploader/display name'] or item['Uploader username']}]({item['Source page']}) · {item['Match type']}")
    else:
        st.info("No same-title ccMixter records were returned for this test.")
    cc_diag = st.session_state.get("ccmixter_diagnostics", {})
    with st.expander("ccMixter API diagnostics"):
        st.write(f"HTTP status: {cc_diag.get('api_status', 'not available')}")
        st.write(f"CSV rows received for the last checked track: {cc_diag.get('rows_received', 0)}")
        st.write(f"Same-title records: {cc_diag.get('same_title_records', 0)}")
        st.write(f"Exact title + artist/uploader metadata matches: {cc_diag.get('exact_title_artist_matches', 0)}")
        st.write(f"Possible cover/remix clues: {cc_diag.get('possible_cover_remix_records', 0)}")
        st.write(f"Same-title-only records: {cc_diag.get('same_title_only_records', 0)}")
        if cc_diag.get("columns"):
            st.write("Returned columns:", ", ".join(str(c) for c in cc_diag["columns"]))
        if cc_diag.get("sample_rows"):
            st.write("Sample metadata from the last checked track:")
            st.dataframe(pd.DataFrame(cc_diag["sample_rows"]), use_container_width=True, hide_index=True)
        if cc_diag.get("error"):
            st.error(cc_diag["error"])

st.caption("Important: exact metadata matches do not establish copyright ownership or recording identity. Internet Archive, Openverse, and ccMixter listings are discovery-only and are never treated as authorized downloads by this app. Jamendo download eligibility is based on Jamendo's API permission flag and should still be checked on the source page.")
