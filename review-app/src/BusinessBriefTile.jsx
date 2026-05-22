import { useState } from "react";
import { parseBullets } from "./helpers";

const GITHUB_OWNER = "peachyinsurance";
const GITHUB_REPO  = "newsletters";
const MAX_PHOTOS   = 3;   // cap GIF length at 3 frames

export default function BusinessBriefTile({ business, onApprove, approving, approved, token }) {
  const localStatus = business._localStatus;
  const bullets     = parseBullets(business.scoring_notes);
  const total       = business.relevance_score ? parseInt(business.relevance_score) : null;

  // Gallery — start with the saved Photo URL first (if it's still one of
  // the candidates), then the rest of the Places photos.
  const candidateList = Array.isArray(business.image_candidates) ? business.image_candidates : [];
  const gallery = [];
  const seen = new Set();
  // If business.image_url is a candidate (static photo), surface it first
  if (business.image_url && candidateList.includes(business.image_url)) {
    gallery.push(business.image_url);
    seen.add(business.image_url);
  }
  for (const u of candidateList) {
    if (u && !seen.has(u)) {
      gallery.push(u);
      seen.add(u);
    }
  }

  // Multi-select state. Default to the saved Photo URL if it's a single
  // candidate; otherwise leave empty so the reviewer makes a fresh pick.
  const initialSelected = business.image_url && candidateList.includes(business.image_url)
    ? [business.image_url]
    : [];
  const [selected, setSelected] = useState(initialSelected);
  const [applied,  setApplied]  = useState(initialSelected);
  const [saving,   setSaving]   = useState(false);
  const [saveMsg,  setSaveMsg]  = useState("");

  const sameSelection = (a, b) =>
    a.length === b.length && a.every((u, i) => u === b[i]);
  const isDirty = selected.length > 0 && !sameSelection(selected, applied);

  function toggle(url) {
    if (saving) return;
    setSaveMsg("");
    setSelected(prev => {
      if (prev.includes(url)) {
        return prev.filter(u => u !== url);
      }
      if (prev.length >= MAX_PHOTOS) {
        // Replace the oldest selection so the user keeps clicking
        // without having to deselect manually.
        return [...prev.slice(1), url];
      }
      return [...prev, url];
    });
  }

  async function handleSave() {
    if (saving || !isDirty || !token) return;
    setSaving(true);
    setSaveMsg(selected.length > 1 ? "Building GIF…" : "Saving…");
    try {
      const res = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/select_business_image.yml/dispatches`,
        {
          method:  "POST",
          headers: {
            Accept:        "application/vnd.github+json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            ref:    "main",
            inputs: {
              source_url: business.source_url || "",
              image_urls: selected.join(","),
              newsletter: business.newsletter_name || "",
            },
          }),
        }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setApplied(selected);
      setSaveMsg(selected.length > 1 ? "GIF queued ✓" : "Saved ✓");
      setTimeout(() => setSaveMsg(""), 4000);
    } catch (e) {
      setSaveMsg(`Save failed: ${e.message}`);
    } finally {
      setSaving(false);
    }
  }

  const buttonLabel = saving
    ? (selected.length > 1 ? "Building GIF…" : "Saving…")
    : isDirty
      ? (selected.length > 1 ? `Build GIF (${selected.length} photos)` : "Save photo")
      : (applied.length > 1 ? "GIF saved ✓" : "Photo saved ✓");

  // For the big preview at the top, show the first selected photo so
  // the reviewer sees what'll lead the gif. Fall back to the current
  // saved Photo URL otherwise.
  const previewImg = selected[0] || business.image_url || gallery[0] || "";

  return (
    <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      {previewImg && (
        <img
          className="tile-image"
          src={previewImg}
          alt={business.business_name || "Business photo"}
          onError={(e) => { e.currentTarget.style.display = "none"; }}
        />
      )}
      {gallery.length > 1 && (
        <div className="image-picker">
          <div className="image-picker-label">
            {`${gallery.length} photo options · pick 1 for a static photo or 2-3 for a GIF`}
            {isDirty && !saveMsg && <span className="image-picker-status image-picker-pending"> · unsaved</span>}
            {saveMsg && (
              <span className={`image-picker-status ${saveMsg.includes("failed") ? "image-picker-error" : ""}`}>
                {" · "}{saveMsg}
              </span>
            )}
          </div>
          <div className="image-picker-strip">
            {gallery.map((u, i) => {
              const isSel = selected.includes(u);
              const order = isSel ? selected.indexOf(u) + 1 : null;
              return (
                <button
                  key={u}
                  type="button"
                  className={`image-thumb ${isSel ? "selected" : ""}`}
                  onClick={() => toggle(u)}
                  disabled={saving}
                  title={isSel ? `Selected (#${order} in GIF)` : `Select option ${i + 1}`}
                  style={isSel ? { position: "relative" } : undefined}
                >
                  <img
                    src={u}
                    alt={`Option ${i + 1}`}
                    onError={(e) => { e.currentTarget.parentElement.style.display = "none"; }}
                  />
                  {order && (
                    <span style={{
                      position: "absolute",
                      top: 4, left: 4,
                      background: "#2A7F2A",
                      color: "#fff",
                      borderRadius: "50%",
                      width: 22, height: 22,
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 13,
                      fontWeight: 700,
                    }}>{order}</span>
                  )}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            className="btn btn-approve-image"
            onClick={handleSave}
            disabled={!isDirty || saving || selected.length === 0}
          >
            {buttonLabel}
          </button>
        </div>
      )}
      <div className="tile-body">
        <div className="tile-meta">
          {business.city         && <span>📍 {business.city}</span>}
          {business.price_level  && <span className="tile-price">💰 {business.price_level}</span>}
        </div>
        <div className="tile-name">{business.business_name}</div>
        {total !== null && !Number.isNaN(total) && (
          <div className="score-bar">
            <div className="score-total">{total}<span>/10</span></div>
          </div>
        )}
        {bullets.length > 0 && (
          <div className="scoring-notes">
            <div className="scoring-notes-label">Why feature this business</div>
            <ul>{bullets.map((b, i) => <li key={i}>{b}</li>)}</ul>
          </div>
        )}
        <div className="tile-blurb">{business.blurb}</div>
        <div className="tile-info">
          {business.address    && <div>{business.address}</div>}
          {business.hours      && <div>🕐 {business.hours}</div>}
          {business.source_url && <a className="tile-link" href={business.source_url} target="_blank" rel="noreferrer">Visit business →</a>}
        </div>
        {!approved && (
          <button className="btn btn-approve" onClick={() => onApprove(business)} disabled={!!approving}>
            {approving === business.source_url ? "Approving..." : "Approve this business"}
          </button>
        )}
      </div>
    </div>
  );
}
