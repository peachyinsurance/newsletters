import {useState} from "react";
import {parseBullets} from "./helpers";

const GITHUB_OWNER = "peachyinsurance";
const GITHUB_REPO  = "newsletters";

export default function FeaturedEventTile({event, onApprove, approving, approved, token}) {
    const localStatus = event._localStatus;
    const bullets = parseBullets(event.scoring_notes);
    const total = event.total_score ? parseInt(event.total_score) : null;

    // Build the gallery: featured image first, then any additional candidates.
    const candidateList = Array.isArray(event.image_candidates) ? event.image_candidates : [];
    const gallery = [];
    const seenInGallery = new Set();
    if (event.image_url) {
        gallery.push(event.image_url);
        seenInGallery.add(event.image_url);
    }
    for (const u of candidateList) {
        if (u && !seenInGallery.has(u)) {
            gallery.push(u);
            seenInGallery.add(u);
        }
    }

    const savedImage = event.image_url || gallery[0] || "";
    const [selectedImage, setSelectedImage] = useState(savedImage);
    const [saving, setSaving]   = useState(false);
    const [saveMsg, setSaveMsg] = useState("");
    // Track the most-recently-saved value separately from `event.image_url`
    // so the UI reflects an approval even before the next data refresh.
    const [appliedImage, setAppliedImage] = useState(savedImage);

    const isDirty = selectedImage && selectedImage !== appliedImage;

    function handlePickImage(imgUrl) {
        // Local-only browse: just preview, no save.
        if (saving) return;
        setSelectedImage(imgUrl);
        setSaveMsg("");
    }

    async function handleApproveImage() {
        if (saving || !isDirty) return;
        setSaving(true);
        setSaveMsg("Saving...");
        try {
            const res = await fetch(
                `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/select_image.yml/dispatches`,
                {
                    method:  "POST",
                    headers: {
                        Accept:        "application/vnd.github+json",
                        Authorization: `Bearer ${token}`,
                    },
                    body: JSON.stringify({
                        ref: "main",
                        inputs: {
                            source_url: event.source_url || "",
                            image_url:  selectedImage,
                            newsletter: event.newsletter_name || "",
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
                alt={event.event_name || "Event image"}
                onError={(e) => { e.currentTarget.style.display = "none"; }}
            />
        )}
        {event.header_image_url && (
            <div className="header-preview">
                <div className="header-preview-label">Newsletter header preview</div>
                <img
                    className="header-preview-img"
                    src={event.header_image_url}
                    alt="Newsletter header composite"
                    onError={(e) => { e.currentTarget.style.display = "none"; }}
                />
            </div>
        )}
        {gallery.length > 1 && (
            <div className="image-picker">
                <div className="image-picker-label">
                    {gallery.length} image options
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
                            title={
                                u === appliedImage
                                    ? "Current saved image"
                                    : `Preview option ${i + 1} of ${gallery.length}`
                            }
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
                    {saving ? "Saving..." : isDirty ? "Approve this image" : "Image approved ✓"}
                </button>
            </div>
        )}
        <div className="tile-body">
            <div className = "tile-meta">
                {event.date && <span>📅 {event.date}</span>}
                {event.time && <span>🕐 {event.time}</span>}
                {event.price && <span className = "tile-price">💰 {event.price}</span>}
            </div>
            <div className = "tile-name">{event.event_name}</div>
            {total !== null && (
                <div className = "score-bar">
                    <div className = "score-total">{total}<span>/30</span></div>
                    <div className = "score-pills">
                        {event.demographic_fit_score && <span className="score-pill">🎯 Demo {event.demographic_fit_score}</span>}
                        {event.uniqueness_score      && <span className="score-pill">✨ Unique {event.uniqueness_score}</span>}
                        {event.audience_match_score  && <span className="score-pill">👥 Match {event.audience_match_score}</span>}
                    </div>
                </div>
            )}
            {bullets.length > 0 && (
                <div className = "scoring-notes">
                    <div className = "scoring-notes-label">Why feature this event</div>
                    <ul>{bullets.map((b,i) => <li key={i}>{b}</li>)}</ul>
                </div>
            )}
            <div className = "tile-info">
                {event.venue     && <div>📍 {event.venue}</div>}
                {event.source_url && <a className="tile-link" href = {event.source_url} target = "_blank" rel="noreferrer">View event details →</a>}
            </div>
            {event.ticket_url && (
                <a className = "btn btn-maps" href = {event.ticket_url} target = "_blank" rel="noreferrer">
                    🎟 Get tickets
                </a>
            )}
            {!approved && (
                <button className = "btn btn-approve" onClick={() => onApprove(event)} disabled={!!approving}>
                    {approving === event.source_url ? "Approving..." : "Approve this event"}
                </button>
            )}
        </div>
    </div>
    );
}