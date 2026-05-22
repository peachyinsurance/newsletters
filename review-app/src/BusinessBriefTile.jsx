import { useState } from "react";
import { parseBullets } from "./helpers";

const GITHUB_OWNER = "peachyinsurance";
const GITHUB_REPO  = "newsletters";

export default function BusinessBriefTile({ business, onApprove, approving, approved, token }) {
  const localStatus = business._localStatus;
  const bullets     = parseBullets(business.scoring_notes);
  const total       = business.relevance_score ? parseInt(business.relevance_score) : null;

  // Gallery — current photo first, then the rest of the Places candidates.
  const candidateList = Array.isArray(business.image_candidates) ? business.image_candidates : [];
  const gallery = [];
  const seen = new Set();
  if (business.image_url) {
    gallery.push(business.image_url);
    seen.add(business.image_url);
  }
  for (const u of candidateList) {
    if (u && !seen.has(u)) {
      gallery.push(u);
      seen.add(u);
    }
  }

  const saved = business.image_url || gallery[0] || "";
  const [selectedImage, setSelectedImage] = useState(saved);
  const [appliedImage,  setAppliedImage]  = useState(saved);
  const [saving, setSaving]   = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

  const isDirty = selectedImage && selectedImage !== appliedImage;

  function handlePickImage(imgUrl) {
    if (saving) return;
    setSelectedImage(imgUrl);
    setSaveMsg("");
  }

  async function handleApproveImage() {
    if (saving || !isDirty || !token) return;
    setSaving(true);
    setSaveMsg("Saving...");
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
              image_url:  selectedImage,
              newsletter: business.newsletter_name || "",
            },
          }),
        }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setAppliedImage(selectedImage);
      setSaveMsg("Saved ✓");
      setTimeout(() => setSaveMsg(""), 3000);
    } catch (e) {
      setSaveMsg(`Save failed: ${e.message}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      {selectedImage && (
        <img
          className="tile-image"
          src={selectedImage}
          alt={business.business_name || "Business photo"}
          onError={(e) => { e.currentTarget.style.display = "none"; }}
        />
      )}
      {gallery.length > 1 && (
        <div className="image-picker">
          <div className="image-picker-label">
            {gallery.length} photo options
            {isDirty && !saveMsg && <span className="image-picker-status image-picker-pending"> · unsaved preview</span>}
            {saveMsg && (
              <span className={`image-picker-status ${saveMsg.includes("failed") ? "image-picker-error" : ""}`}>
                {" · "}{saveMsg}
              </span>
            )}
          </div>
          <div className="image-picker-strip">
            {gallery.map((u, i) => (
              <button
                key={u}
                type="button"
                className={`image-thumb ${u === selectedImage ? "selected" : ""} ${u === appliedImage ? "applied" : ""}`}
                onClick={() => handlePickImage(u)}
                disabled={saving}
                title={u === appliedImage ? "Current saved photo" : `Preview option ${i + 1} of ${gallery.length}`}
              >
                <img
                  src={u}
                  alt={`Option ${i + 1}`}
                  onError={(e) => { e.currentTarget.parentElement.style.display = "none"; }}
                />
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn btn-approve-image"
            onClick={handleApproveImage}
            disabled={!isDirty || saving}
          >
            {saving ? "Saving..." : isDirty ? "Approve this photo" : "Photo approved ✓"}
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
