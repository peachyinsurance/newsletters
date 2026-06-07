export default function InSearchOfTile({ job, onApprove, approving, approved }) {
  const localStatus = job._localStatus;
  const body = job.description || job.scraped_snippet || "";
  const isBonus = job.bonus === "yes";

  const tileClasses = [
    "tile",
    localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={tileClasses}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
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
        {!approved && (
          <button className="btn btn-approve" onClick={() => onApprove(job)} disabled={!!approving}>
            {approving === job.job_listings_url ? "Approving..." : "Approve this listing"}
          </button>
        )}
      </div>
    </div>
  );
}
