import { parseBullets } from "./helpers";

export default function InsuranceTipTile({ tip, onApprove, approving, approved }) {
  const localStatus = tip._localStatus;
  const bullets     = parseBullets(tip.scoring_notes);
  const total       = tip.total_score ? parseInt(tip.total_score) : null;

  const tileClasses = [
    "tile",
    localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={tileClasses}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      <div className="tile-body">
        <div className="tile-meta">
          {tip.topic && <span className="tile-cuisine">{tip.topic}</span>}
          {tip.category && <span className="tile-cuisine">{tip.category}</span>}
        </div>
        <div className="tile-name">{tip.tip_title}</div>
        {total !== null && (
          <div className="score-bar">
            <div className="score-total">{total}<span>/30</span></div>
            <div className="score-pills">
              {tip.relevance_score && <span className="score-pill">🎯 Relevance {tip.relevance_score}</span>}
              {tip.actionability_score && <span className="score-pill">✅ Actionable {tip.actionability_score}</span>}
              {tip.timeliness_score && <span className="score-pill">⏰ Timely {tip.timeliness_score}</span>}
            </div>
          </div>
        )}
        {bullets.length > 0 && (
          <div className="scoring-notes">
            <div className="scoring-notes-label">Why this tip</div>
            <ul>{bullets.map((b, i) => <li key={i}>{b}</li>)}</ul>
          </div>
        )}
        <div className="tile-blurb">{tip.blurb}</div>
        <div className="tile-info">
          {tip.source_name && <div>Source: {tip.source_name}</div>}
          {tip.source_url && <a className="tile-link" href={tip.source_url} target="_blank" rel="noreferrer">View source →</a>}
          {tip.sponsor_name && <div>Brought to you by {tip.sponsor_name}</div>}
        </div>
        {!approved && (
          <button className="btn btn-approve" onClick={() => onApprove(tip)} disabled={!!approving}>
            {approving === tip.source_url ? "Approving..." : "Approve this tip"}
          </button>
        )}
      </div>
    </div>
  );
}
