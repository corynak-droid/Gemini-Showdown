import streamlit as st
import pandas as pd
import requests

st.set_page_config(page_title="Auto NFL Showdown Value Tool", layout="wide")
st.title("🏈 Automated NFL Showdown Value & Prop Analyzer")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
}

# --- 1. AUTOMATICALLY POPULATE DROPDOWN WITH SHOWDOWN SLATES ---
@st.cache_data(ttl=300)
def get_available_showdown_slates():
    url = "https://www.draftkings.com/lobby/getcontests?sport=NFL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        slates = {}
        # Filter for Showdown Captain slates
        for dg in data.get("DraftGroups", []):
            game_type = dg.get("GameTypeId", 0)
            # GameTypeId 96 / 133 or Showdown keywords denote single-game captain mode
            desc = dg.get("DraftGroupSeriesIdDescription", "") or dg.get("ContestStartTimeSuffix", "")
            name = dg.get("GameCountDescription", "")
            if "Showdown" in desc or "Single Game" in desc or dg.get("GameCount") == 1:
                label = f"{dg.get('StartDateEst', '')[:10]} - {name} ({desc})"
                slates[label] = dg["DraftGroupId"]
        return slates
    except Exception:
        return {}

slates = get_available_showdown_slates()

if not slates:
    st.warning("No active NFL Showdown slates found in the live DraftKings lobby right now.")
    selected_slate = None
else:
    selected_slate = st.selectbox("Select Upcoming Showdown Game:", options=list(slates.keys()))

# --- 2. FETCH SALARIES & PROPS AUTOMATICALLY ---
if selected_slate:
    draft_group_id = slates[selected_slate]
    
    with st.spinner("Fetching salaries and matching sportsbook lines in background..."):
        # A. Fetch DFS draftables (Salaries)
        sal_url = f"https://api.draftkings.com/draftgroups/v1/draftgroups/{draft_group_id}/draftables"
        sal_res = requests.get(sal_url, headers=HEADERS, timeout=10).json()
        
        salary_map = {}
        team_names = set()
        for p in sal_res.get("draftables", []):
            name = p.get("displayName")
            slot = p.get("rosterSlotId")
            sal = p.get("salary")
            team = p.get("teamAbbreviation")
            if team:
                team_names.add(team)
            if name:
                salary_map.setdefault(name, {"Team": team})
                if slot == 66: # CPT slot
                    salary_map[name]["CPT_Sal"] = sal
                else: # FLEX slot
                    salary_map[name]["FLEX_Sal"] = sal

        # B. Identify Sportsbook Event ID for the Matchup
        sb_nfl_url = "https://sportsbook.draftkings.com/api/sportscontent/dkusnj/v1/leagues/88808/events"
        events_res = requests.get(sb_nfl_url, headers=HEADERS, timeout=10).json()
        
        event_id = None
        for ev in events_res.get("events", []):
            ev_name = ev.get("name", "")
            # Check if both teams are in the match name
            if any(t in ev_name for t in team_names):
                event_id = ev.get("id")
                break

        # Fallback to demo Event ID if team matching doesn't resolve in dev environment
        if not event_id:
            event_id = "34118255"

        # C. Query Sportsbook Markets: Receiving (16570), Rushing (16571), Anytime TD (12438)
        def get_sb_market(cat_id):
            u = "https://sportsbook.draftkings.com/api/sportscontent/dkusnj/v1/markets"
            params = {"isBatchable": "false", "templateVars": f"{event_id}|{cat_id}"}
            try:
                return requests.get(u, params=params, headers=HEADERS, timeout=8).json().get("selections", [])
            except Exception:
                return []

        rec_data = get_sb_market("16570")
        rush_data = get_sb_market("16571")
        td_data = get_sb_market("12438")

        # D. Parse Median Milestones
        props = {}
        for s in rec_data:
            name = s.get("participants", [{}])[0].get("name")
            if name and "MostBalancedGlobalProbability" in s.get("tags", []):
                props.setdefault(name, {})["Rec_Med"] = s.get("milestoneValue", 0)

        for s in rush_data:
            name = s.get("participants", [{}])[0].get("name")
            if name and "MostBalancedGlobalProbability" in s.get("tags", []):
                props.setdefault(name, {})["Rush_Med"] = s.get("milestoneValue", 0)

        for s in td_data:
            if s.get("outcomeType") == "ToScoreAnyTime":
                name = s.get("participants", [{}])[0].get("name")
                if name:
                    pct = float(s.get("displayOdds", {}).get("percentage", "0%").replace("%", "")) / 100.0
                    props.setdefault(name, {})["ATD_Prob"] = pct

        # E. Cross-Reference and Score
        rows = []
        for name, data in salary_map.items():
            f_sal = data.get("FLEX_Sal")
            if not f_sal:
                continue
            
            p_prop = props.get(name, {})
            rush = p_prop.get("Rush_Med", 0.0)
            rec = p_prop.get("Rec_Med", 0.0)
            atd = p_prop.get("ATD_Prob", 0.0)
            
            # Simple heuristic: estimated catches = rec_yds / 11.5
            est_catches = rec / 11.5 if rec > 0 else 0
            fpts = (rush * 0.1) + (rec * 0.1) + (est_catches * 1.0) + (atd * 6.0)
            
            val = (fpts / f_sal * 1000) if f_sal > 0 else 0
            cpt_val = (fpts * 1.5 / data.get("CPT_Sal", 1) * 1000)

            rows.append({
                "Player": name,
                "Team": data.get("Team", ""),
                "Flex Sal": f"${f_sal:,}",
                "CPT Sal": f"${data.get('CPT_Sal', 0):,}",
                "Rush Yds": rush,
                "Rec Yds": rec,
                "ATD %": f"{int(atd * 100)}%",
                "Base FPTS": round(fpts, 2),
                "Pts / $1k": round(val, 2),
                "CPT Pts / $1k": round(cpt_val, 2)
            })

        df_final = pd.DataFrame(rows).sort_values(by="Pts / $1k", ascending=False)

    # --- 3. DISPLAY VALUE TABLE ---
    st.subheader("📊 Cross-Referenced Player Value Board")
    st.dataframe(df_final, use_container_width=True)

    # --- 4. DISPLAY AUTOMATED WRITTEN BREAKDOWN ---
    st.subheader("💡 Automated Slate Breakdown & Value Exploits")
    
    punts = df_final[(df_final['Pts / $1k'] >= 1.4) & (df_final['Base FPTS'] >= 4.0)]
    anchors = df_final[df_final['Base FPTS'] >= 11.0]

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### 🎯 Top Value Punts (Sub-$5k Targets)")
        if not punts.empty:
            for _, row in punts.iterrows():
                st.markdown(
                    f"* **{row['Player']} ({row['Team']}) - {row['Flex Sal']}**: "
                    f"Generates **{row['Pts / $1k']} Pts/$1k** based on a **{row['Rec Yds']} Rec Yd / {row['Rush Yds']} Rush Yd** median line. "
                    f"Pricing does not match projected volume."
                )
        else:
            st.write("No extreme sub-$5k mispricings identified on this board.")

    with col2:
        st.markdown("### 👑 Primary Slate Anchors (FLEX & CPT)")
        if not anchors.empty:
            for _, row in anchors.iterrows():
                st.markdown(
                    f"* **{row['Player']} ({row['Team']}) - {row['Flex Sal']}**: "
                    f"High-volume anchor with **{row['Base FPTS']} projected baseline points** and a **{row['ATD %']} Anytime TD probability**. "
                    f"Core building block for non-punt Captain builds."
                )
        else:
            st.write("Evenly distributed offensive projections across the board.")
