import { parseBullets } from "./helpers";

export default function PetTile({ pet, onApprove, approving, approved }) {
  const localStatus = pet._localStatus;
  const bullets     = parseBullets(pet.scoring_notes);
  const total       = pet.total_score ? parseInt(pet.total_score) : null;

  return (
    <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      <div className="tile-photo">
        {pet.photo_url ? <img src={pet.photo_url} alt={pet.pet_name} /> : <span>No photo available</span>}
      </div>
      <div className="tile-body">
        <div className="tile-meta">
          <span className="tile-shelter">{pet.shelter_name}</span>
        </div>
        <div className="tile-name">{pet.pet_name}</div>
        {total !== null && (
          <div className="score-bar">
            <div className="score-total">{total}<span>/30</span></div>
            <div className="score-pills">
              {pet.adoptability_score && <span className="score-pill">🏠 Adoptability {pet.adoptability_score}</span>}
              {pet.story_score        && <span className="score-pill">📖 Story {pet.story_score}</span>}
              {pet.shelter_time_score && <span className="score-pill">⏱ Wait {pet.shelter_time_score}</span>}
            </div>
          </div>
        )}
        {bullets.length > 0 && (
          <div className="scoring-notes">
            <div className="scoring-notes-label">Why feature this pet</div>
            <ul>{bullets.map((b, i) => <li key={i}>{b}</li>)}</ul>
          </div>
        )}
        <div className="tile-blurb">{pet.blurb}</div>
        <div className="tile-info">
          {pet.shelter_address && <div>{pet.shelter_address}</div>}
          {pet.shelter_phone   && <div>{pet.shelter_phone}{pet.shelter_email ? ` | ${pet.shelter_email}` : ""}</div>}
          {pet.shelter_hours   && <div>{pet.shelter_hours}</div>}
          {pet.source_url      && <a className="tile-link" href={pet.source_url} target="_blank" rel="noreferrer">View listing →</a>}
        </div>
        {!approved && (
          <button className="btn btn-approve" onClick={() => onApprove(pet)} disabled={!!approving}>
            {approving === pet.source_url ? "Approving..." : "Approve this pet"}
          </button>
        )}
      </div>
    </div>
  );
}
