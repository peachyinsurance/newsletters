import { useState, useEffect, useRef } from "react";
import "./styles.css";
import { isOddWeek, checkPassword } from "./helpers";
import PetTile from "./PetTile";
import RestaurantTile from "./RestaurantTile";

// ── CONFIG ────────────────────────────────────────────────────────────────────
const GITHUB_OWNER = "couch2coders";
const GITHUB_REPO  = "NewsletterAutomation";

// ── SECTIONS CONFIG ───────────────────────────────────────────────────────────
const SECTIONS = {
  pets: {
    dataFile:        "pets.json",
    idField:         "source_url",
    nameField:       "pet_name",
    approveWorkflow: "approve_pet.yml",
    approveInputs:   (item) => ({ source_url: item.source_url }),
    redoWorkflow:    "redo_pets.yml",
    storageKey:      "approved_pet_ids",
    sectionPrefix:   "pets",
    TileComponent:   PetTile,
    itemPropName:    "pet",
    label:           "Pets",
    navIcon:         "\uD83D\uDC3E",
    loadingText:     "Loading this week's candidates...",
    emptyText:       "No pending pets found. Run the pipeline to generate new candidates.",
    statusBarText:   (count, extra) => `${count} ${extra.weekType} candidates this week`,
    emptyCandidatesText: (_extra) => { const wt = _extra.weekType; return { title: `No ${wt} candidates`, sub: "Run the pipeline to generate new candidates." }; },
    filterCandidates: (items) => {
      const oddWeek  = isOddWeek();
      const weekType = oddWeek ? "cat" : "dog";
      return {
        candidates: items.filter(p => (p.animal_type || "").toLowerCase() === weekType),
        extra: { oddWeek, weekType },
      };
    },
    renderDefaultWinners: (visibleItems, extra) => {
      const overallWinner = visibleItems.find(p => p.default_winner === "yes");
      const catWinner     = visibleItems.find(p => p.cat_default === "yes");
      const dogWinner     = visibleItems.find(p => p.dog_default === "yes");
      return {
        label: `Default Winners \u2014 ${extra.oddWeek ? "Odd Week (Cat Week)" : "Even Week (Dog Week)"}`,
        rows: [
          { badgeClass: "winner-badge-overall", badgeText: "Overall",
            name: overallWinner ? `${overallWinner.pet_name} (${overallWinner.animal_type})` : "None set",
            score: overallWinner ? `${overallWinner.total_score}/30` : null },
          { badgeClass: "winner-badge-cat", badgeText: "Cat",
            name: catWinner ? catWinner.pet_name : "None set",
            score: catWinner ? `${catWinner.total_score}/30` : null },
          { badgeClass: "winner-badge-dog", badgeText: "Dog",
            name: dogWinner ? dogWinner.pet_name : "None set",
            score: dogWinner ? `${dogWinner.total_score}/30` : null },
        ],
      };
    },
    header: {
      eyebrow:    "Newsletter Pet Review",
      h1Prefix:   "Pick This Week's",
      h1Emphasis: "Featured Friend",
      sub:        "Review candidates and approve the one that best fits the newsletter.",
    },
  },

  restaurants: {
    dataFile:        "restaurants.json",
    idField:         "place_id",
    nameField:       "restaurant_name",
    approveWorkflow: "approve_restaurant.yml",
    approveInputs:   (item) => ({ place_id: item.place_id }),
    redoWorkflow:    "redo_restaurants.yml",
    storageKey:      "approved_restaurant_ids",
    sectionPrefix:   "restaurants",
    TileComponent:   RestaurantTile,
    itemPropName:    "restaurant",
    label:           "Restaurants",
    navIcon:         "\uD83C\uDF7D",
    loadingText:     "Loading this week's restaurant candidates...",
    emptyText:       "No pending restaurants found. Run the pipeline to generate new candidates.",
    statusBarText:   (count) => `${count} restaurant candidates this week`,
    emptyCandidatesText: () => ({ title: "No candidates", sub: "Run the pipeline to generate new restaurant candidates." }),
    filterCandidates: (items) => ({ candidates: items, extra: {} }),
    renderDefaultWinners: (visibleItems) => {
      const defaultWinner = visibleItems.find(r => r.default_winner === "yes");
      return {
        label: "Default Winner",
        rows: [
          { badgeClass: "winner-badge-rest", badgeText: "Restaurant",
            name: defaultWinner ? defaultWinner.restaurant_name : "None set",
            score: defaultWinner ? `${defaultWinner.total_score}/40` : null },
        ],
      };
    },
    header: {
      eyebrow:    "Newsletter Restaurant Review",
      h1Prefix:   "Pick This Week's",
      h1Emphasis: "Featured Restaurant",
      sub:        "Review candidates and approve the one that best fits the newsletter.",
    },
  },
};

// ── GENERIC REVIEW PAGE ──────────────────────────────────────────────────────
function ReviewPage({ config, token, onApprove, onUnapprove, approvedSections, onNewslettersLoaded }) {
  const [items, setItems]                   = useState([]);
  const [newsletters, setNewsletters]       = useState([]);
  const [selectedNewsletter, setNewsletter] = useState("");
  const [loading, setLoading]               = useState(true);
  const [approving, setApproving]           = useState(null);
  const [error, setError]                   = useState("");
  const [success, setSuccess]               = useState("");
  const [redoing, setRedoing]               = useState(false);
  const [approvedMap, setApprovedMap]       = useState(() => {
    const map = {};
    const savedApprovals = JSON.parse(localStorage.getItem(config.storageKey) || "{}");
    Object.keys(approvedSections).forEach(key => {
      if (key.startsWith(config.sectionPrefix + ":")) {
        const nl = key.replace(config.sectionPrefix + ":", "");
        map[nl] = savedApprovals[nl] || "__previously_approved__";
      }
    });
    return map;
  });

  const pollRef    = useRef(null);
  const timeoutRef = useRef(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  useEffect(() => { fetchData(); }, []);

  async function fetchData() {
    setLoading(true);
    setError("");
    try {
      const res  = await fetch(`/NewsletterAutomation/${config.dataFile}`, { cache: "no-store" });
      const rows = await res.json();

      const allNames = [...new Set(rows.map(r => r.newsletter_name).filter(Boolean))];

      const withStatus = rows.map(item => {
        const s = (item.status || "").toLowerCase();
        if (s === "approved") return { ...item, _localStatus: "approved" };
        if (s === "rejected") return { ...item, _localStatus: "rejected" };
        return item;
      });

      // Build approvedMap from the data (source of truth)
      const dataApprovedMap = {};
      withStatus.forEach(item => {
        if (item._localStatus === "approved" && item.newsletter_name) {
          dataApprovedMap[item.newsletter_name] = item[config.idField];
        }
      });
      // Merge: keep in-session approvals, but let data override
      setApprovedMap(prev => {
        const merged = {};
        // Keep in-session approvals (from handleApprove clicks)
        Object.keys(prev).forEach(nl => { merged[nl] = prev[nl]; });
        // Data overrides: if data says approved, use that; if data says all pending, clear it
        allNames.forEach(nl => {
          if (dataApprovedMap[nl]) {
            merged[nl] = dataApprovedMap[nl];
          }
          // Only clear if data explicitly shows all pending AND we didn't just approve in this session
          // (don't clear — let handleRedo's poll handle clearing)
        });
        return merged;
      });
      // Sync section checkmarks
      allNames.forEach(nl => {
        if (dataApprovedMap[nl]) onApprove(nl);
      });

      setNewsletters(allNames);
      if (allNames.length > 0) setNewsletter(prev => prev || allNames[0]);
      setItems(withStatus);
      onNewslettersLoaded(allNames);
    } catch (e) {
      setError("Could not load data.");
    } finally {
      setLoading(false);
    }
  }

  async function handleApprove(item) {
    if (!token) return;
    const itemId = item[config.idField];
    setApproving(itemId);
    setError("");
    try {
      const res = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${config.approveWorkflow}/dispatches`,
        { method: "POST", headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: config.approveInputs(item) }) }
      );
      if (!res.ok) { const err = await res.json(); throw new Error(err.message || "GitHub API error"); }
      setApprovedMap(prev => ({ ...prev, [selectedNewsletter]: itemId }));
      setSuccess({ newsletter: selectedNewsletter, name: item[config.nameField] });
      const savedApprovals = JSON.parse(localStorage.getItem(config.storageKey) || "{}");
      savedApprovals[selectedNewsletter] = itemId;
      localStorage.setItem(config.storageKey, JSON.stringify(savedApprovals));
      setItems(prev => prev.map(i => {
        if (i.newsletter_name !== selectedNewsletter) return i;
        return { ...i, _localStatus: i[config.idField] === itemId ? "approved" : "rejected" };
      }));
      onApprove(selectedNewsletter);
    } catch (e) {
      setError(`Approval failed: ${e.message}`);
    } finally {
      setApproving(null);
    }
  }

  async function handleRedo() {
    if (!token) return;
    setError("");
    setRedoing(true);
    try {
      const res = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${config.redoWorkflow}/dispatches`,
        { method: "POST", headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: { newsletter_name: selectedNewsletter } }) }
      );
      if (!res.ok) { const err = await res.json(); throw new Error(err.message || "GitHub API error"); }
      pollRef.current = setInterval(async () => {
        try {
          // Poll from raw GitHub to bypass GitHub Pages CDN cache
          const rawUrl = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/gh-pages/${config.dataFile}?t=${Date.now()}`;
          const r    = await fetch(rawUrl, { cache: "no-store" });
          const rows = await r.json();
          const nlItems = rows.filter(i => i.newsletter_name === selectedNewsletter);
          const hasApproved = nlItems.some(i => (i.status || "").toLowerCase() === "approved");
          if (!hasApproved && nlItems.length > 0) {
            clearInterval(pollRef.current);
            pollRef.current = null;
            setApprovedMap(prev => { const next = { ...prev }; delete next[selectedNewsletter]; return next; });
            onUnapprove(selectedNewsletter);
            const savedApprovals = JSON.parse(localStorage.getItem(config.storageKey) || "{}");
            delete savedApprovals[selectedNewsletter];
            localStorage.setItem(config.storageKey, JSON.stringify(savedApprovals));
            setRedoing(false);
            const allRows = rows.map(item => {
              const s = (item.status || "").toLowerCase();
              if (s === "approved") return { ...item, _localStatus: "approved" };
              if (s === "rejected") return { ...item, _localStatus: "rejected" };
              return { ...item, _localStatus: undefined };
            });
            setItems(allRows);
          }
        } catch {}
      }, 4000);
      timeoutRef.current = setTimeout(() => {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        setRedoing(false);
      }, 300000);
    } catch (e) {
      setError(`Redo failed: ${e.message}`);
      setRedoing(false);
    }
  }

  const visibleItems = items.filter(i => i.newsletter_name === selectedNewsletter);
  const { candidates: unsortedCandidates, extra } = config.filterCandidates(visibleItems);

  // Sort candidates by total_score descending (highest first)
  const candidates = [...unsortedCandidates].sort((a, b) => {
    const scoreA = parseInt(a.total_score) || 0;
    const scoreB = parseInt(b.total_score) || 0;
    return scoreB - scoreA;
  });

  const winners = config.renderDefaultWinners(visibleItems, extra);
  const TileComponent = config.TileComponent;
  const emptyMsg = config.emptyCandidatesText(extra);

  // Split candidates into approved winner and others
  const winnerId      = approvedMap[selectedNewsletter];
  const approvedTile  = winnerId ? candidates.find(i => i[config.idField] === winnerId) : null;
  const otherTiles    = winnerId ? candidates.filter(i => i[config.idField] !== winnerId) : candidates;

  if (loading) return <div className="loading">{config.loadingText}</div>;
  if (items.length === 0 && newsletters.length === 0 && Object.keys(approvedMap).length === 0) return (
    <div className="empty"><h2>All clear!</h2><p>{config.emptyText}</p></div>
  );

  return (
    <>
      {success && success.newsletter === selectedNewsletter && <div className="success-banner"><strong>Approved!</strong>{success.name} approved!</div>}
      {error   && <div className="error-msg" style={{marginBottom: 24}}>{error}</div>}

      {newsletters.length > 0 && (
        <div style={{marginBottom: 32, textAlign: "center"}}>
          <select className="newsletter-select" value={selectedNewsletter} onChange={e => setNewsletter(e.target.value)}>
            {newsletters.map(n => (
              <option key={n} value={n}>
                {approvedSections?.[`${config.sectionPrefix}:${n}`] ? `\u2705 ${n.replace(/_/g, " ")}` : n.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="default-winners">
        <div className="default-winners-label">{winners.label}</div>
        <div className="default-winners-rows">
          {winners.rows.map((row, i) => (
            <div className="default-winner-row" key={i}>
              <span className={`winner-badge ${row.badgeClass}`}>{row.badgeText}</span>
              <span className="winner-name">{row.name}</span>
              {row.score && <span className="winner-score">{row.score}</span>}
            </div>
          ))}
        </div>
      </div>

      <hr className="divider" />

      <div className="status-bar">
        <strong>{config.statusBarText(candidates.length, extra)}</strong> &mdash; select one to feature
      </div>

      {winnerId && (
        <>
          <div className="status-bar" style={{background: "#EFF7F0", border: "1px solid #C0DFC4", marginBottom: 16}}>
            <strong>{"\u2705"} Winner selected{approvedTile ? `: ${approvedTile[config.nameField]}` : ""}!</strong> — approved and sent to Notion
          </div>
          <div style={{textAlign: "center", marginBottom: 24}}>
            <button className="btn btn-redo" onClick={handleRedo} disabled={redoing}>
              {redoing ? "\u23F3 Resetting candidates..." : "\uD83D\uDD04 Redo Selection"}
            </button>
            {redoing && <p style={{marginTop: 12, fontSize: 13, color: "#6B5744"}}>Updating Notion and refreshing data, this may take a minute...</p>}
          </div>

          {approvedTile && (
            <div className="winner-highlight">
              <div className="tiles">
                <TileComponent {...{[config.itemPropName]: approvedTile}} onApprove={handleApprove} approving={approving} approved={winnerId} />
              </div>
            </div>
          )}

          {otherTiles.length > 0 && (
            <>
              <div className="other-candidates-label">Other Candidates</div>
              <div className="tiles">
                {otherTiles.map((item, idx) => (
                  <TileComponent key={item[config.idField] || idx} {...{[config.itemPropName]: item}} onApprove={handleApprove} approving={approving} approved={winnerId} />
                ))}
              </div>
            </>
          )}
        </>
      )}

      {!winnerId && (
        candidates.length === 0 ? (
          <div className="empty"><h2>{emptyMsg.title}</h2><p>{emptyMsg.sub}</p></div>
        ) : (
          <div className="tiles">
            {candidates.map((item, idx) => (
              <TileComponent key={item[config.idField] || idx} {...{[config.itemPropName]: item}} onApprove={handleApprove} approving={approving} approved={null} />
            ))}
          </div>
        )
      )}
    </>
  );
}

// ── MAIN APP ──────────────────────────────────────────────────────────────────
const SECTION_KEYS       = Object.keys(SECTIONS);
const EXPECTED_NEWSLETTERS = 2;

export default function App() {
  const [token, setToken]           = useState(() => localStorage.getItem("gh_token") || "");
  const [tokenInput, setTokenInput] = useState("");
  const [error, setError]           = useState("");
  const [activePage, setActivePage] = useState(SECTION_KEYS[0]);
  const [step, setStep]             = useState("password");
  const [approvedSections, setApprovedSections] = useState(() => {
    try { return JSON.parse(localStorage.getItem("approved_sections") || "{}"); }
    catch { return {}; }
  });
  const [sectionNewsletters, setSectionNewsletters] = useState({});

  const isAuthed = Boolean(token);

  function markApproved(section, newsletter) {
    const key     = `${section}:${newsletter}`;
    const updated = { ...approvedSections, [key]: true };
    setApprovedSections(updated);
    localStorage.setItem("approved_sections", JSON.stringify(updated));
  }

  function markUnapproved(section, newsletter) {
    const key     = `${section}:${newsletter}`;
    const updated = { ...approvedSections };
    delete updated[key];
    setApprovedSections(updated);
    localStorage.setItem("approved_sections", JSON.stringify(updated));
  }

  async function handleTokenSubmit() {
    if (step === "password") {
      const valid = await checkPassword(tokenInput.trim());
      if (valid) {
        setStep("token");
        setTokenInput("");
        setError("");
      } else {
        setError("Incorrect password.");
      }
    } else {
      if (!tokenInput.trim()) return;
      localStorage.setItem("gh_token", tokenInput.trim());
      setToken(tokenInput.trim());
      setTokenInput("");
      setError("");
    }
  }

  function handleNewslettersLoaded(sectionKey, newsletters) {
    setSectionNewsletters(prev => ({ ...prev, [sectionKey]: newsletters }));
  }

  const pages = SECTION_KEYS.map(key => {
    const cfg    = SECTIONS[key];
    const nlList = sectionNewsletters[key] || [];
    const allApproved = nlList.length >= EXPECTED_NEWSLETTERS &&
      nlList.every(n => approvedSections[`${cfg.sectionPrefix}:${n}`]);
    return { id: key, label: `${allApproved ? "\u2705 " : ""}${cfg.navIcon} ${cfg.label}` };
  });

  const currentConfig = SECTIONS[activePage];
  const currentHeader = currentConfig.header;

  return (
    <div className="app">
      {!isAuthed ? (
        <>
          <div className="header">
            <p className="header-eyebrow">Newsletter Review</p>
            <h1>Pick This Week's<br/><em>Best Content</em></h1>
            <p className="header-sub">Review and approve content for your newsletters.</p>
          </div>
          <div className="token-gate">
            <h2>Sign In</h2>
            <p>{step === "password" ? "Enter your password to get started." : "Enter your GitHub token to enable approvals."}</p>
            <input
              className="token-input"
              type="password"
              placeholder={step === "password" ? "Enter password" : "ghp_xxxxxxxxxxxx"}
              value={tokenInput}
              onChange={e => setTokenInput(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleTokenSubmit()}
            />
            <button className="btn btn-primary" onClick={handleTokenSubmit}>Continue</button>
            {error && <div className="error-msg">{error}</div>}
          </div>
        </>
      ) : (
        <div className="app-layout">
          <div className="nav-bar">
            <div className="nav-tabs">
              {pages.map(p => (
                <button key={p.id} className={`nav-btn ${activePage === p.id ? "active" : ""}`} onClick={() => setActivePage(p.id)}>
                  {p.label}
                </button>
              ))}
            </div>
            <div className="nav-select-wrap">
              <select className="nav-select" value={activePage} onChange={e => setActivePage(e.target.value)}>
                {pages.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </div>
          </div>

          <div className="header app-header">
            <p className="header-eyebrow">{currentHeader.eyebrow}</p>
            <h1>{currentHeader.h1Prefix}<br/><em>{currentHeader.h1Emphasis}</em></h1>
            <p className="header-sub">{currentHeader.sub}</p>
          </div>

          <div className="app-content">
            <ReviewPage
              key={activePage}
              config={currentConfig}
              token={token}
              onApprove={(n) => markApproved(currentConfig.sectionPrefix, n)}
              onUnapprove={(n) => markUnapproved(currentConfig.sectionPrefix, n)}
              approvedSections={approvedSections}
              onNewslettersLoaded={(nls) => handleNewslettersLoaded(activePage, nls)}
            />
          </div>
        </div>
      )}
    </div>
  );
}
