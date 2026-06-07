import { useState, useEffect, useRef } from "react";
import "./styles.css";
import { isOddWeek, checkPassword, decodeBase64Utf8 } from "./helpers";
import PetTile from "./PetTile";
import RestaurantTile from "./RestaurantTile";
import FeaturedEventTile from "./FeaturedEventTile";
import BusinessBriefTile from "./BusinessBriefTile";
import MemeTile from "./MemeTile";


// ── CONFIG ────────────────────────────────────────────────────────────────────
const GITHUB_OWNER = "peachyinsurance";
const GITHUB_REPO  = "newsletters";

// ── SECTIONS CONFIG ───────────────────────────────────────────────────────────
const SECTIONS = {
  pets: {
    dataFile:        "pets.json",
    idField:         "source_url",
    nameField:       "pet_name",
    approveWorkflow: "approve_pet.yml",
    approveInputs:   (item) => ({ source_url: item.source_url, newsletter: item.newsletter_name || "" }),
    redoWorkflow:    (newsletter) => `redo_${newsletter.toLowerCase()}.yml`,
    redoSection:     "pets",
    approvedStatus:  "approved",
    rejectedStatus:  "rejected",
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
    approveInputs:   (item) => ({ place_id: item.place_id, newsletter: item.newsletter_name || "" }),
    redoWorkflow:    (newsletter) => `redo_${newsletter.toLowerCase()}.yml`,
    redoSection:     "restaurants",
    approvedStatus:  "Tier 1 Winner",
    rejectedStatus:  "Tier 2 Winner",
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

  events: {
    dataFile:        "events.json",
    idField:         "source_url",
    nameField:       "event_name",
    approveWorkflow: "approve_featured_event.yml",
    approveInputs:   (item) => ({ source_url: item.source_url, newsletter: item.newsletter_name || "" }),
    redoWorkflow:    (newsletter) => `redo_${newsletter.toLowerCase()}.yml`,
    redoSection:     "events",
    approvedStatus:  "approved",
    rejectedStatus:  "rejected",
    storageKey:      "approved_event_ids",
    sectionPrefix:   "events",
    TileComponent:   FeaturedEventTile,
    itemPropName:    "event",
    label:           "Featured Events",
    navIcon:         "\uD83C\uDFAB",
    loadingText:     "Loading this week's event candidates...",
    emptyText:       "No pending events found. Run the pipeline to generate new candidates.",
    statusBarText:   (count) => `${count} event candidates this week`,
    emptyCandidatesText: () => ({ title: "No candidates", sub: "Run the pipeline to generate new event candidates." }),
    filterCandidates: (items) => ({ candidates: items, extra: {} }),
    renderDefaultWinners: (visibleItems) => {
      const defaultWinner = visibleItems.find(e => e.default_winner === "yes");
      return {
        label: "Default Winner",
        rows: [
          { badgeClass: "winner-badge-overall", badgeText: "Event",
            name: defaultWinner ? defaultWinner.event_name : "None set",
            score: defaultWinner ? `${defaultWinner.total_score}/30` : null },
        ],
      };
    },
    header: {
      eyebrow:    "Newsletter Featured Event Review",
      h1Prefix:   "Pick This Week's",
      h1Emphasis: "Featured Event",
      sub:        "Review candidates and approve the one that best fits the newsletter.",
    },
  },

  business_briefs: {
    dataFile:        "business_briefs.json",
    idField:         "source_url",
    nameField:       "business_name",
    approveWorkflow: "approve_business_brief.yml",
    approveInputs:   (item) => ({ source_url: item.source_url, newsletter: item.newsletter_name || "" }),
    redoWorkflow:    (newsletter) => `redo_${newsletter.toLowerCase()}.yml`,
    redoSection:     "business_brief",
    approvedStatus:  "approved",
    rejectedStatus:  "rejected",
    storageKey:      "approved_business_ids",
    sectionPrefix:   "business_briefs",
    TileComponent:   BusinessBriefTile,
    itemPropName:    "business",
    label:           "Business Brief",
    navIcon:         "🏢",
    loadingText:     "Loading this week's business candidates...",
    emptyText:       "No pending businesses found. Run the Business Brief pipeline.",
    statusBarText:   (count) => `${count} business candidates this week`,
    emptyCandidatesText: () => ({ title: "No candidates", sub: "Run the Business Brief pipeline." }),
    filterCandidates: (items) => ({ candidates: items, extra: {} }),
    renderDefaultWinners: (visibleItems) => {
      const defaultWinner = visibleItems.find(b => b.default_winner === "yes");
      return {
        label: "Default Winner",
        rows: [
          { badgeClass: "winner-badge-rest", badgeText: "Business",
            name: defaultWinner ? defaultWinner.business_name : "None set",
            score: defaultWinner ? `${defaultWinner.relevance_score}/10` : null },
        ],
      };
    },
    header: {
      eyebrow:    "Newsletter Business Brief Review",
      h1Prefix:   "Pick This Week's",
      h1Emphasis: "Featured Business",
      sub:        "Review the three candidates, pick the photo that fits, and approve.",
    },
  },

  memes: {
    dataFile:        "memes.json",
    idField:         "permalink",
    nameField:       "caption",
    approveWorkflow: "approve_meme.yml",
    approveInputs:   (item) => ({ permalink: item.permalink, newsletter: item.newsletter_name || "" }),
    redoWorkflow:    (newsletter) => `redo_${newsletter.toLowerCase()}.yml`,
    redoSection:     "meme",
    approvedStatus:  "approved",
    rejectedStatus:  "rejected",
    storageKey:      "approved_meme_ids",
    sectionPrefix:   "memes",
    TileComponent:   MemeTile,
    itemPropName:    "meme",
    label:           "Memes",
    navIcon:         "😂",
    loadingText:     "Loading this week's meme candidates...",
    emptyText:       "No pending memes found. Run the Meme Corner scraper to fetch fresh candidates.",
    statusBarText:   (count) => `${count} meme candidates this week — pick up to 4`,
    emptyCandidatesText: () => ({ title: "No candidates", sub: "Run the Meme Corner scraper." }),
    filterCandidates: (items) => ({ candidates: items, extra: {} }),
    renderDefaultWinners: () => ({ label: "Approval Progress", rows: [] }),
    // Multi-select awareness — see handleApprove + render below.
    multiSelect:     true,
    maxApprovals:    4,
    rejectRemainingWorkflow: "approve_meme.yml",
    header: {
      eyebrow:    "Newsletter Meme Corner Review",
      h1Prefix:   "Pick This Week's",
      h1Emphasis: "Memes (up to 4)",
      sub:        "Approve up to 4 memes. After your picks, click \"Reject the rest\" to clear remaining pending rows.",
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
      // Fetch via GitHub Contents API — always returns latest committed data, no caching
      const ghHeaders = token
        ? { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" }
        : { Accept: "application/vnd.github+json" };
      const fileUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${config.dataFile}?ref=gh-pages&_=${Date.now()}`;
      const res = await fetch(fileUrl, { headers: ghHeaders, cache: "no-store" });
      if (!res.ok) throw new Error("Could not fetch data");
      const fileInfo = await res.json();
      const rows = JSON.parse(decodeBase64Utf8(fileInfo.content));

      const allNames = [...new Set(rows.map(r => r.newsletter_name).filter(Boolean))];

      const approvedLower = config.approvedStatus.toLowerCase();
      const rejectedLower = config.rejectedStatus.toLowerCase();
      const withStatus = rows.map(item => {
        const s = (item.status || "").toLowerCase();
        if (s === approvedLower) return { ...item, _localStatus: "approved" };
        if (s === rejectedLower) return { ...item, _localStatus: "rejected" };
        return item;
      });

      // Build approvedMap purely from data (source of truth).
      // For multi-select sections (memes) the value is an ARRAY of ids;
      // for single-select sections it's a single id.
      const dataApprovedMap = {};
      withStatus.forEach(item => {
        if (item._localStatus === "approved" && item.newsletter_name) {
          if (config.multiSelect) {
            const arr = dataApprovedMap[item.newsletter_name] || [];
            arr.push(item[config.idField]);
            dataApprovedMap[item.newsletter_name] = arr;
          } else {
            dataApprovedMap[item.newsletter_name] = item[config.idField];
          }
        }
      });
      // Sync localStorage to match data — clear stale entries
      const savedApprovals = {};
      Object.keys(dataApprovedMap).forEach(nl => { savedApprovals[nl] = dataApprovedMap[nl]; });
      localStorage.setItem(config.storageKey, JSON.stringify(savedApprovals));

      setApprovedMap(() => ({ ...dataApprovedMap }));
      // Sync section checkmarks
      allNames.forEach(nl => {
        if (dataApprovedMap[nl]) onApprove(nl);
        else onUnapprove(nl);
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
    const ghHeaders = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" };
    try {
      // 1. Fetch current JSON from gh-pages
      const fileUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${config.dataFile}?ref=gh-pages&_=${Date.now()}`;
      const fileRes = await fetch(fileUrl, { headers: ghHeaders, cache: "no-store" });
      if (!fileRes.ok) throw new Error("Could not fetch data file from gh-pages");
      const fileInfo = await fileRes.json();
      const rows = JSON.parse(decodeBase64Utf8(fileInfo.content));

      // 2. Set statuses for this newsletter (uses section-specific status names).
      // Multi-select: ONLY flip the clicked row to approved; leave other
      // pending rows alone so additional selections can still happen.
      // Single-select: clicked row → approved, other pending → rejected.
      for (const row of rows) {
        if (row.newsletter_name !== selectedNewsletter) continue;
        if (row[config.idField] === itemId) {
          row.status = config.approvedStatus;
        } else if (!config.multiSelect &&
                   (row.status === "pending" || row.status === "Pending")) {
          row.status = config.rejectedStatus;
        }
      }

      // 3. Commit updated JSON back to gh-pages
      const commitRes = await fetch(fileUrl, {
        method: "PUT",
        headers: ghHeaders,
        body: JSON.stringify({
          message: `approve: ${item[config.nameField]} for ${selectedNewsletter}`,
          content: btoa(unescape(encodeURIComponent(JSON.stringify(rows, null, 2)))),
          sha: fileInfo.sha,
          branch: "gh-pages",
        }),
      });
      if (!commitRes.ok) throw new Error("Could not update data file on gh-pages");

      // 4. Update local state immediately. Multi-select appends; single
      //    overwrites.
      setApprovedMap(prev => {
        if (config.multiSelect) {
          const arr = Array.isArray(prev[selectedNewsletter])
            ? [...prev[selectedNewsletter]]
            : [];
          if (!arr.includes(itemId)) arr.push(itemId);
          return { ...prev, [selectedNewsletter]: arr };
        }
        return { ...prev, [selectedNewsletter]: itemId };
      });
      setSuccess({ newsletter: selectedNewsletter, name: item[config.nameField] });
      const savedApprovals = JSON.parse(localStorage.getItem(config.storageKey) || "{}");
      if (config.multiSelect) {
        const arr = Array.isArray(savedApprovals[selectedNewsletter])
          ? [...savedApprovals[selectedNewsletter]] : [];
        if (!arr.includes(itemId)) arr.push(itemId);
        savedApprovals[selectedNewsletter] = arr;
      } else {
        savedApprovals[selectedNewsletter] = itemId;
      }
      localStorage.setItem(config.storageKey, JSON.stringify(savedApprovals));
      const aLower = config.approvedStatus.toLowerCase();
      const rLower = config.rejectedStatus.toLowerCase();
      setItems(rows.map(row => {
        const s = (row.status || "").toLowerCase();
        if (s === aLower) return { ...row, _localStatus: "approved" };
        if (s === rLower) return { ...row, _localStatus: "rejected" };
        return { ...row, _localStatus: undefined };
      }));
      onApprove(selectedNewsletter);

      // 5. Fire-and-forget: dispatch Action to sync Notion in the background
      fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${config.approveWorkflow}/dispatches`,
        { method: "POST", headers: ghHeaders, body: JSON.stringify({ ref: "main", inputs: config.approveInputs(item) }) }
      ).catch(() => {}); // Notion sync is best-effort
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
    const ghHeaders = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" };
    try {
      // 1. Fetch current JSON from gh-pages
      const fileUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${config.dataFile}?ref=gh-pages&_=${Date.now()}`;
      const fileRes = await fetch(fileUrl, { headers: ghHeaders, cache: "no-store" });
      if (!fileRes.ok) throw new Error("Could not fetch data file from gh-pages");
      const fileInfo = await fileRes.json();
      const rows = JSON.parse(decodeBase64Utf8(fileInfo.content));

      // 2. Reset statuses and default_winner for this newsletter to pending
      let changed = 0;
      for (const item of rows) {
        if (item.newsletter_name === selectedNewsletter) {
          if (["approved", "rejected", "Approved", "Rejected", "Tier 1 Winner", "Tier 2 Winner"].includes(item.status)) {
            item.status = "pending";
            changed++;
          }
          item.default_winner = "";
        }
      }

      // 3. Commit updated JSON back to gh-pages
      const commitRes = await fetch(fileUrl, {
        method: "PUT",
        headers: ghHeaders,
        body: JSON.stringify({
          message: `redo: reset ${selectedNewsletter} ${config.redoSection} to pending`,
          content: btoa(unescape(encodeURIComponent(JSON.stringify(rows, null, 2)))),
          sha: fileInfo.sha,
          branch: "gh-pages",
        }),
      });
      if (!commitRes.ok) throw new Error("Could not update data file on gh-pages");

      // 4. Update local state immediately
      setApprovedMap(prev => { const next = { ...prev }; delete next[selectedNewsletter]; return next; });
      onUnapprove(selectedNewsletter);
      const savedApprovals = JSON.parse(localStorage.getItem(config.storageKey) || "{}");
      delete savedApprovals[selectedNewsletter];
      localStorage.setItem(config.storageKey, JSON.stringify(savedApprovals));
      setItems(rows.map(item => {
        const s = (item.status || "").toLowerCase();
        if (s === "approved") return { ...item, _localStatus: "approved" };
        if (s === "rejected") return { ...item, _localStatus: "rejected" };
        return { ...item, _localStatus: undefined };
      }));
      setRedoing(false);

      // 5. Fire-and-forget: dispatch Action to sync Notion in the background
      const workflowFile = config.redoWorkflow(selectedNewsletter);
      fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${workflowFile}/dispatches`,
        { method: "POST", headers: ghHeaders, body: JSON.stringify({ ref: "main", inputs: { section: config.redoSection } }) }
      ).catch(() => {}); // Notion sync is best-effort
    } catch (e) {
      setError(`Redo failed: ${e.message}`);
      setRedoing(false);
    }
  }

  const visibleItems = items.filter(i => i.newsletter_name === selectedNewsletter);
  const { candidates: unsortedCandidates, extra } = config.filterCandidates(visibleItems);

  // Sort candidates by total_score descending (highest first). For memes
  // total_score isn't set, so the parseInt falls back to 0 and the
  // fallback below sorts by Reddit `score` instead.
  const candidates = [...unsortedCandidates].sort((a, b) => {
    if (config.multiSelect) {
      return (parseInt(b.score) || 0) - (parseInt(a.score) || 0);
    }
    return (parseInt(b.total_score) || 0) - (parseInt(a.total_score) || 0);
  });

  const winners = config.renderDefaultWinners(visibleItems, extra);
  const TileComponent = config.TileComponent;
  const emptyMsg = config.emptyCandidatesText(extra);

  // Multi-select: approvedMap[nl] is an array of ids; max is config.maxApprovals.
  // Single-select: approvedMap[nl] is one id (current behavior).
  const approvedIdsArr = config.multiSelect
    ? (Array.isArray(approvedMap[selectedNewsletter]) ? approvedMap[selectedNewsletter] : [])
    : [];
  const approvedCount  = config.multiSelect ? approvedIdsArr.length : 0;
  const maxApprovals   = config.maxApprovals || Infinity;
  const limitReached   = config.multiSelect && approvedCount >= maxApprovals;
  const winnerId       = config.multiSelect ? null : approvedMap[selectedNewsletter];
  const approvedTile   = winnerId ? candidates.find(i => i[config.idField] === winnerId) : null;
  const otherTiles     = winnerId ? candidates.filter(i => i[config.idField] !== winnerId) : candidates;

  // "Reject the Rest" handler — fires the approve workflow in
  // reject-remaining mode so all pending rows for this newsletter get
  // flipped to rejected, leaving only the approved picks.
  async function handleRejectRest() {
    if (!token || !config.multiSelect) return;
    setError("");
    setRedoing(true);
    const ghHeaders = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" };
    try {
      // 1. Local JSON: flip every pending row for this newsletter to rejected.
      const fileUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${config.dataFile}?ref=gh-pages&_=${Date.now()}`;
      const fileRes = await fetch(fileUrl, { headers: ghHeaders, cache: "no-store" });
      if (!fileRes.ok) throw new Error("Could not fetch data file from gh-pages");
      const fileInfo = await fileRes.json();
      const rows = JSON.parse(decodeBase64Utf8(fileInfo.content));
      for (const row of rows) {
        if (row.newsletter_name !== selectedNewsletter) continue;
        if (row.status === "pending" || row.status === "Pending") {
          row.status = config.rejectedStatus;
        }
      }
      const commitRes = await fetch(fileUrl, {
        method: "PUT",
        headers: ghHeaders,
        body: JSON.stringify({
          message: `reject-rest: ${selectedNewsletter} ${config.redoSection}`,
          content: btoa(unescape(encodeURIComponent(JSON.stringify(rows, null, 2)))),
          sha: fileInfo.sha,
          branch: "gh-pages",
        }),
      });
      if (!commitRes.ok) throw new Error("Could not update data file on gh-pages");

      // 2. Update local items.
      const aLower = config.approvedStatus.toLowerCase();
      const rLower = config.rejectedStatus.toLowerCase();
      setItems(rows.map(row => {
        const s = (row.status || "").toLowerCase();
        if (s === aLower) return { ...row, _localStatus: "approved" };
        if (s === rLower) return { ...row, _localStatus: "rejected" };
        return { ...row, _localStatus: undefined };
      }));

      // 3. Fire-and-forget Notion sync.
      fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${config.rejectRemainingWorkflow}/dispatches`,
        {
          method: "POST",
          headers: ghHeaders,
          body: JSON.stringify({
            ref: "main",
            inputs: {
              newsletter:           selectedNewsletter,
              reject_remaining:     "true",
              approved_permalinks:  approvedIdsArr.join(","),
            },
          }),
        }
      ).catch(() => {});
    } catch (e) {
      setError(`Reject-the-rest failed: ${e.message}`);
    } finally {
      setRedoing(false);
    }
  }

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
          {config.multiSelect ? (
            <div className="default-winner-row">
              <span className="winner-badge winner-badge-overall">
                {approvedCount} / {maxApprovals} approved
              </span>
              {limitReached && (
                <span className="winner-name" style={{color: "#2A7F2A"}}>
                  Limit reached — click "Reject the rest" below to finalize
                </span>
              )}
            </div>
          ) : (
            winners.rows.map((row, i) => (
              <div className="default-winner-row" key={i}>
                <span className={`winner-badge ${row.badgeClass}`}>{row.badgeText}</span>
                <span className="winner-name">{row.name}</span>
                {row.score && <span className="winner-score">{row.score}</span>}
              </div>
            ))
          )}
        </div>
      </div>

      <hr className="divider" />

      <div className="status-bar">
        <strong>{config.statusBarText(candidates.length, extra)}</strong>
        {!config.multiSelect && " — select one to feature"}
      </div>

      {config.multiSelect && approvedCount > 0 && (
        <div style={{textAlign: "center", margin: "16px 0 24px"}}>
          <button className="btn btn-redo" onClick={handleRejectRest} disabled={redoing}>
            {redoing ? "⏳ Rejecting…" : "✗ Reject the rest"}
          </button>
          <div style={{marginTop: 8, fontSize: 13, color: "#6B5744"}}>
            Flips every remaining pending meme for this newsletter to rejected.
          </div>
        </div>
      )}

      {!config.multiSelect && winnerId && (
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
                <TileComponent {...{[config.itemPropName]: approvedTile}} onApprove={handleApprove} approving={approving} approved={winnerId} token={token} />
              </div>
            </div>
          )}

          {otherTiles.length > 0 && (
            <>
              <div className="other-candidates-label">Other Candidates</div>
              <div className="tiles">
                {otherTiles.map((item, idx) => (
                  <TileComponent key={item[config.idField] || idx} {...{[config.itemPropName]: item}} onApprove={handleApprove} approving={approving} approved={winnerId} token={token} />
                ))}
              </div>
            </>
          )}
        </>
      )}

      {(config.multiSelect || !winnerId) && (
        candidates.length === 0 ? (
          <div className="empty"><h2>{emptyMsg.title}</h2><p>{emptyMsg.sub}</p></div>
        ) : (
          <div className="tiles">
            {candidates.map((item, idx) => {
              const id = item[config.idField];
              const isApproved = config.multiSelect && approvedIdsArr.includes(id);
              return (
                <TileComponent
                  key={id || idx}
                  {...{[config.itemPropName]: item}}
                  onApprove={handleApprove}
                  approving={approving}
                  approved={isApproved ? id : null}
                  disableApprove={config.multiSelect && limitReached && !isApproved}
                  token={token}
                />
              );
            })}
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
  const [refreshing, setRefreshing]   = useState(false);
  const [refreshStatus, setRefreshStatus] = useState("");
  const [refreshError, setRefreshError]   = useState("");

  const isAuthed = Boolean(token);

  function handleSignOut() {
    if (refreshing) return;
    if (!confirm("Sign out? Your saved token will be cleared.")) return;
    localStorage.removeItem("gh_token");
    setToken("");
    setStep("password");
    setTokenInput("");
    setError("");
  }

  async function handleRefresh() {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshError("");
    setRefreshStatus("Triggering data refresh...");
    try {
      // 1. Dispatch the Deploy Review App workflow
      const dispatchRes = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/deploy_review_app.yml/dispatches`,
        {
          method:  "POST",
          headers: {
            "Accept":               "application/vnd.github+json",
            "Authorization":        `Bearer ${token}`,
            "X-GitHub-Api-Version": "2022-11-28",
          },
          body: JSON.stringify({ ref: "main" }),
        }
      );
      if (!dispatchRes.ok) {
        const detail = await dispatchRes.text().catch(() => "");
        if (dispatchRes.status === 401) {
          throw new Error("Your GitHub token expired or doesn't have permission. Sign out and sign in with a fresh token (scopes: repo + workflow).");
        }
        if (dispatchRes.status === 404) {
          throw new Error("Workflow not found on main. Make sure deploy_review_app.yml is committed and pushed.");
        }
        throw new Error(`Dispatch failed: HTTP ${dispatchRes.status}. ${detail.slice(0, 200)}`);
      }

      // 2. Poll the workflow's recent runs until the dispatched run completes.
      const dispatchedAt = Date.now();
      const MAX_WAIT_MS  = 5 * 60 * 1000;   // 5 min hard cap
      const POLL_MS      = 4000;
      let foundRunId = null;
      let conclusion = null;

      while (Date.now() - dispatchedAt < MAX_WAIT_MS) {
        await new Promise(r => setTimeout(r, POLL_MS));
        const runsRes = await fetch(
          `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/deploy_review_app.yml/runs?event=workflow_dispatch&per_page=5`,
          {
            headers: {
              "Accept":               "application/vnd.github+json",
              "Authorization":        `Bearer ${token}`,
              "X-GitHub-Api-Version": "2022-11-28",
            },
          }
        );
        if (!runsRes.ok) continue;
        const data = await runsRes.json();
        // Find the most recent run created after our dispatch
        const candidate = (data.workflow_runs || []).find(
          r => new Date(r.created_at).getTime() >= dispatchedAt - 30000
        );
        if (!candidate) {
          setRefreshStatus("Waiting for workflow to start...");
          continue;
        }
        foundRunId = candidate.id;
        if (candidate.status === "completed") {
          conclusion = candidate.conclusion;
          break;
        }
        setRefreshStatus(`Workflow running (${candidate.status})...`);
      }

      if (!foundRunId) {
        throw new Error("Workflow did not start within the time limit.");
      }
      if (conclusion !== "success") {
        throw new Error(`Workflow finished with status: ${conclusion || "unknown"}`);
      }

      // 3. Reload the page so all section data re-fetches from gh-pages.
      setRefreshStatus("Refresh complete. Reloading...");
      // Small buffer so the gh-pages CDN has time to serve fresh JSON
      await new Promise(r => setTimeout(r, 1500));
      window.location.reload();
    } catch (err) {
      setRefreshError(String(err.message || err));
      setRefreshing(false);
      setRefreshStatus("");
    }
  }

  function markApproved(section, newsletter) {
    const key = `${section}:${newsletter}`;
    setApprovedSections(prev => {
      const updated = { ...prev, [key]: true };
      localStorage.setItem("approved_sections", JSON.stringify(updated));
      return updated;
    });
  }

  function markUnapproved(section, newsletter) {
    const key = `${section}:${newsletter}`;
    setApprovedSections(prev => {
      const updated = { ...prev };
      delete updated[key];
      localStorage.setItem("approved_sections", JSON.stringify(updated));
      return updated;
    });
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
          {refreshing && (
            <div className="refresh-overlay" aria-live="polite">
              <div className="refresh-dialog">
                <div className="refresh-spinner" />
                <p className="refresh-text">{refreshStatus || "Refreshing data..."}</p>
                <p className="refresh-sub">This may take 30-60 seconds. Don't close this tab.</p>
              </div>
            </div>
          )}
          <div className="refresh-bar">
            <button
              className="btn btn-refresh"
              onClick={handleRefresh}
              disabled={refreshing}
              title="Sync Notion → review app (runs Deploy Review App workflow)"
            >
              {refreshing ? "Refreshing..." : "↻ Refresh data"}
            </button>
            <button
              className="btn btn-signout"
              onClick={handleSignOut}
              disabled={refreshing}
              title="Clear your saved GitHub token and sign back in"
            >
              Sign out
            </button>
            {refreshError && <span className="refresh-error">⚠ {refreshError}</span>}
          </div>
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
