export default function InSearchOfTile({ job, onApprove, approved, disableApprove }) {
  // `approved` carries this row's id when it's part of the current selection.
  const selected = !!approved;
  const localStatus = job._localStatus;
  const body = job.description || job.scraped_snippet || "";
  const isBonus = job.bonus === "yes";

  const tileClasses = [
    "tile",
    selected ? "approved" : localStatus === "rejected" ? "rejected" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={tileClasses}>
      {selected && <div className="tile-badge">✓ Selected</div>}
      <div className="tile-body">
        <div className="tile-meta">
          {job.city && <span className="tile-cuisine">📍 {job.city}</span>}
          {isBonus && <span className="tile-cuisine">🎁 Bonus resource</span>}
        </div>
        <div className="tile-name">{job.employer}</div>
        {job.roles && (
          <div className="scoring-notes">
            <div className="scoring-notes-label">Roles</div>
            <div>{job.roles}</div>
          </div>
        )}
        <div className="tile-blurb">{body}</div>
        <div className="tile-info">
          {job.job_listings_url && (
            <a className="tile-link" href={job.job_listings_url} target="_blank" rel="noreferrer">
              View posting →
            </a>
          )}
        </div>
        {/* Toggle: select adds to the pick list; clicking again removes it.
            Nothing is committed until "Submit selection". */}
        <button
          className={selected ? "btn btn-redo" : "btn btn-approve"}
          onClick={() => onApprove(job)}
          disabled={!selected && disableApprove}
        >
          {selected ? "✓ Selected — click to remove" : "Select this listing"}
        </button>
      </div>
    </div>
  );
}
