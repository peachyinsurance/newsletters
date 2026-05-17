export default function MemeTile({ meme, onApprove, approving, approved, disableApprove }) {
  const localStatus = meme._localStatus;
  const score = meme.score ? parseInt(meme.score) : null;

  return (
    <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      {localStatus === "rejected" && <div className="tile-badge tile-badge-rejected">✗ Rejected</div>}
      <div className="tile-photo">
        {meme.image_url
          ? <img src={meme.image_url} alt={meme.caption || "meme"} />
          : <span>No image available</span>}
      </div>
      <div className="tile-body">
        <div className="tile-meta">
          <span className="tile-shelter">r/{meme.subreddit}</span>
          {meme.reddit_author && <span style={{marginLeft: 8, color: "#888"}}>by u/{meme.reddit_author}</span>}
        </div>
        <div className="tile-name" style={{fontSize: "1rem", lineHeight: 1.3}}>{meme.caption}</div>
        {score !== null && !Number.isNaN(score) && (
          <div className="score-bar">
            <div className="score-total">{score.toLocaleString()}<span> upvotes</span></div>
          </div>
        )}
        {meme.permalink && (
          <div className="tile-info">
            <a className="tile-link" href={meme.permalink} target="_blank" rel="noreferrer">
              View on Reddit →
            </a>
          </div>
        )}
        {!approved && (
          <button
            className="btn btn-approve"
            onClick={() => onApprove(meme)}
            disabled={!!approving || disableApprove}
            title={disableApprove ? "You've reached the 4-meme limit. Unapprove one first." : ""}
          >
            {approving === meme.permalink
              ? "Approving..."
              : disableApprove
                ? "4-meme limit reached"
                : "Approve this meme"}
          </button>
        )}
      </div>
    </div>
  );
}
