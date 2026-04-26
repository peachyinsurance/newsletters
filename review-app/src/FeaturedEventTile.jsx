import {parseBullets} from "./helpers";

export default function FeaturedEventTile({event, onApprove, approving, approved}) {
    const localStatus = event._localStatus;
    const bullets = parseBullets(event.scoring_notes);
    const total = event.total_score ? parseInt(event.total_score) : null;

    return (
        <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
        {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
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
            <div className = "tile-blurb">{event.blurb}</div>
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