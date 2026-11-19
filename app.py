"""RepoLens UI: paste a GitHub repo, watch its architecture evolve."""
import os
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network
import core

st.set_page_config(page_title="RepoLens", layout="wide")
try:
    os.environ.setdefault("GEMINI_API_KEY", st.secrets["GEMINI_API_KEY"])
except Exception:
    pass

st.title("RepoLens")
st.caption("How a codebase's architecture changed over time, generated from its code and git history.")


@st.cache_data(show_spinner=False)
def run(url):
    status = st.status("Analyzing...")
    result = core.analyze(url, progress=status.write)
    status.update(label="Done", state="complete")
    return result


url = st.text_input("Public GitHub repo URL", placeholder="https://github.com/psf/requests")
demos = sorted(p.stem.replace("__", "/") for p in core.CACHE.glob("*.json")) if core.CACHE.exists() else []
cols = st.columns(len(demos) + 1)
if cols[0].button("Analyze", type="primary") and url:
    st.session_state.url = url
for col, d in zip(cols[1:], demos):
    if col.button(d):
        st.session_state.url = f"https://github.com/{d}"

if "url" not in st.session_state:
    st.stop()
try:
    data = run(st.session_state.url)
except Exception as e:
    st.error(f"Analysis failed: {e}")
    st.stop()
snaps = data["snapshots"]
if not snaps:
    st.warning("No supported source files found (.py .js .ts .java .go).")
    st.stop()

st.subheader(data["repo"])
labels = [f'{s["date"]} · {s["sha"]}' for s in snaps]
pick = st.select_slider("Timeline", labels, value=labels[-1])
snap = snaps[labels.index(pick)]
st.info(f'**What changed:** {snap["change_note"]}  \n{snap["n_files"]} files · {len(snap["modules"])} modules')

left, right = st.columns([3, 2])
with left:
    net = Network(height="560px", width="100%", directed=True, cdn_resources="remote")
    net.barnes_hut(gravity=-4000, spring_length=180)
    for m in snap["modules"]:
        net.add_node(m["name"], label=m["name"], title=m["summary"], color=core.color(m["name"]),
                     size=15 + 3 * len(m["files"]) ** 0.7, font={"size": 18})
    for a, b, w in snap["edges"]:
        net.add_edge(a, b, value=w, title=f"{w} imports")
    components.html(net.generate_html(), height=580)

with right:
    names = [m["name"] for m in snap["modules"]]
    mod = snap["modules"][names.index(st.selectbox("Module", names))]
    st.write(mod["summary"])
    st.dataframe([{"file": f["path"], "what it does": f["summary"]} for f in mod["files"]],
                 hide_index=True, use_container_width=True)
    if snap["reading_order"]:
        st.markdown("**Start reading here**\n" + "\n".join(
            f"{i}. `{p}`" for i, p in enumerate(snap["reading_order"], 1)))
