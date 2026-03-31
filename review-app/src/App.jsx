import { useState, useEffect } from "react";

// ── CONFIG ────────────────────────────────────────────────────────────────────
const GITHUB_OWNER         = "couch2coders";
const GITHUB_REPO          = "NewsletterAutomation";
const GITHUB_WORKFLOW_PETS = "approve_pet.yml";
const GITHUB_WORKFLOW_REST = "approve_restaurant.yml";
const APP_PASSWORD         = "Adm1n$$";

// ── STYLES ────────────────────────────────────────────────────────────────────
const styles = `
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;1,400&family=DM+Sans:wght@300;400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --cream:   #F7F3EE;
    --bark:    #2C1A0E;
    --rust:    #C4531A;
    --sage:    #7A9E7E;
    --sand:    #E8DDD0;
    --gold:    #C4931A;
    --shadow:  rgba(44,26,14,0.12);
  }

  body { background: var(--cream); font-family: 'DM Sans', sans-serif; color: var(--bark); min-height: 100vh; }
  .app { max-width: 1200px; margin: 0 auto; padding: 48px 24px; }

  /* ── Nav bar (horizontal on desktop, dropdown on mobile) ── */
  .nav-bar {
    background: white;
    border-radius: 12px;
    box-shadow: 0 2px 12px var(--shadow);
    margin-bottom: 40px;
    overflow: hidden;
  }
  .nav-tabs {
    display: flex;
  }
  .nav-btn {
    flex: 1;
    padding: 14px 20px;
    border: none;
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    background: transparent;
    color: #6B5744;
    border-bottom: 3px solid transparent;
  }
  .nav-btn.active {
    background: var(--cream);
    color: var(--rust);
    border-bottom: 3px solid var(--rust);
  }
  .nav-btn:hover:not(.active) { background: var(--sand); }

  /* Collapse to dropdown on small screens */
  .nav-select-wrap { display: none; padding: 8px; }
  .nav-select {
    width: 100%;
    padding: 10px 16px;
    border-radius: 8px;
    border: 1.5px solid var(--sand);
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    background: var(--cream);
    color: var(--bark);
    cursor: pointer;
    outline: none;
  }
  @media (max-width: 480px) {
    .nav-tabs { display: none; }
    .nav-select-wrap { display: block; }
  }

  /* ── Header ── */
  .header { text-align: center; margin-bottom: 40px; }
  .header-eyebrow { font-family: 'DM Sans', sans-serif; font-weight: 300; font-size: 11px; letter-spacing: 0.25em; text-transform: uppercase; color: var(--rust); margin-bottom: 12px; }
  .header h1 { font-family: 'Playfair Display', serif; font-size: clamp(2rem, 5vw, 3.5rem); font-weight: 700; line-height: 1.1; color: var(--bark); }
  .header h1 em { font-style: italic; color: var(--rust); }
  .header-sub { margin-top: 16px; font-size: 15px; font-weight: 300; color: #6B5744; max-width: 480px; margin-left: auto; margin-right: auto; line-height: 1.6; }

  /* ── Auth ── */
  .token-gate { max-width: 480px; margin: 0 auto; background: white; border-radius: 16px; padding: 40px; box-shadow: 0 4px 32px var(--shadow); text-align: center; }
  .token-gate h2 { font-family: 'Playfair Display', serif; font-size: 1.5rem; margin-bottom: 8px; }
  .token-gate p { font-size: 14px; color: #6B5744; margin-bottom: 24px; line-height: 1.6; }
  .token-input { width: 100%; padding: 12px 16px; border: 1.5px solid var(--sand); border-radius: 8px; font-family: 'DM Sans', sans-serif; font-size: 14px; background: var(--cream); color: var(--bark); margin-bottom: 12px; outline: none; transition: border-color 0.2s; }
  .token-input:focus { border-color: var(--rust); }

  /* ── Buttons ── */
  .btn { display: inline-flex; align-items: center; gap: 8px; padding: 12px 28px; border-radius: 8px; font-family: 'DM Sans', sans-serif; font-size: 14px; font-weight: 500; cursor: pointer; border: none; transition: all 0.2s; }
  .btn-primary { background: var(--rust); color: white; width: 100%; justify-content: center; }
  .btn-primary:hover { background: #A8441A; transform: translateY(-1px); }
  .btn-primary:disabled { background: #C4A090; cursor: not-allowed; transform: none; }
  .btn-approve { background: var(--sage); color: white; width: 100%; justify-content: center; margin-top: 20px; padding: 14px 28px; font-size: 15px; }
  .btn-approve:hover { background: #5F8563; transform: translateY(-1px); }
  .btn-approve:disabled { background: #A8C4AA; cursor: not-allowed; transform: none; }
  .btn-maps { background: #4285F4; color: white; width: 100%; justify-content: center; margin-top: 12px; padding: 12px 28px; font-size: 14px; text-decoration: none; border-radius: 8px; display: inline-flex; align-items: center; gap: 8px; font-weight: 500; transition: all 0.2s; }
  .btn-maps:hover { background: #3367D6; transform: translateY(-1px); }

  /* ── Newsletter select ── */
  .newsletter-select { padding: 10px 20px; border-radius: 8px; border: 1.5px solid var(--sand); font-family: 'DM Sans', sans-serif; font-size: 15px; background: white; color: var(--bark); cursor: pointer; outline: none; }
  .newsletter-select:focus { border-color: var(--rust); }

  /* ── Default winners ── */
  .default-winners { background: white; border-radius: 16px; padding: 24px 28px; margin-bottom: 32px; box-shadow: 0 4px 24px var(--shadow); }
  .default-winners-label { font-size: 11px; font-weight: 500; letter-spacing: 0.2em; text-transform: uppercase; color: var(--rust); margin-bottom: 16px; }
  .default-winners-rows { display: flex; flex-direction: column; gap: 10px; }
  .default-winner-row { display: flex; align-items: center; gap: 12px; }
  .winner-badge { color: white; border-radius: 99px; padding: 2px 10px; font-size: 11px; font-weight: 500; white-space: nowrap; }
  .winner-badge-overall { background: var(--rust); }
  .winner-badge-cat     { background: var(--sage); }
  .winner-badge-dog     { background: var(--sage); }
  .winner-badge-rest    { background: var(--gold); }
  .winner-name  { font-size: 15px; font-weight: 500; }
  .winner-score { font-size: 13px; color: #6B5744; }

  .divider { border: none; border-top: 1px solid var(--sand); margin-bottom: 32px; }

  /* ── Tiles ── */
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 32px; }
  .tile { background: white; border-radius: 20px; overflow: hidden; box-shadow: 0 4px 24px var(--shadow); transition: transform 0.25s, box-shadow 0.25s; display: flex; flex-direction: column; position: relative; }
  .tile:hover { transform: translateY(-4px); box-shadow: 0 12px 40px var(--shadow); }
  .tile.approved { outline: 3px solid var(--sage); outline-offset: -3px; }
  .tile.rejected { opacity: 0.4; pointer-events: none; }
  .tile-badge { position: absolute; top: 16px; right: 16px; background: var(--sage); color: white; font-size: 11px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; padding: 4px 12px; border-radius: 99px; z-index: 2; }
  .tile-photo { width: 100%; height: 240px; background: var(--sand); display: flex; align-items: center; justify-content: center; color: #A89080; font-size: 13px; flex-shrink: 0; }
  .tile-photo img { width: 100%; height: 100%; object-fit: cover; }
  .tile-body { padding: 28px; flex: 1; display: flex; flex-direction: column; }
  .tile-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .tile-shelter { font-size: 11px; font-weight: 500; letter-spacing: 0.15em; text-transform: uppercase; color: var(--rust); }
  .tile-cuisine { font-size: 11px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; color: var(--gold); background: #FFF8ED; border: 1px solid #F5DFA0; border-radius: 99px; padding: 2px 10px; }
  .tile-rating { font-size: 13px; color: #6B5744; display: flex; align-items: center; gap: 4px; }
  .tile-price { font-size: 13px; font-weight: 500; color: var(--sage); }
  .tile-name { font-family: 'Playfair Display', serif; font-size: 1.6rem; font-weight: 700; color: var(--bark); margin-bottom: 16px; line-height: 1.2; }

  /* ── Score bar ── */
  .score-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; padding: 12px 16px; background: var(--cream); border-radius: 10px; }
  .score-total { font-family: 'Playfair Display', serif; font-size: 1.4rem; font-weight: 700; color: var(--rust); white-space: nowrap; }
  .score-total span { font-size: 0.8rem; color: #A89080; font-family: 'DM Sans', sans-serif; font-weight: 300; }
  .score-pills { display: flex; flex-wrap: wrap; gap: 6px; }
  .score-pill { font-size: 11px; font-weight: 500; padding: 3px 8px; border-radius: 99px; background: white; border: 1px solid var(--sand); color: #6B5744; white-space: nowrap; }

  /* ── Scoring notes ── */
  .scoring-notes { margin-bottom: 16px; padding: 14px 16px; background: #F0F7F1; border-radius: 10px; border-left: 3px solid var(--sage); }
  .scoring-notes-label { font-size: 10px; font-weight: 500; letter-spacing: 0.15em; text-transform: uppercase; color: var(--sage); margin-bottom: 8px; }
  .scoring-notes ul { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 6px; }
  .scoring-notes li { font-size: 13px; line-height: 1.5; color: #3A5C3E; }

  /* ── Tile content ── */
  .tile-blurb { font-size: 14px; line-height: 1.75; color: #4A3728; font-weight: 300; flex: 1; white-space: pre-wrap; }
  .tile-info { margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--sand); font-size: 12px; color: #6B5744; line-height: 1.8; }
  .tile-link { display: inline-block; margin-top: 8px; font-size: 12px; color: var(--rust); text-decoration: none; font-weight: 500; }
  .tile-link:hover { text-decoration: underline; }

  /* ── Status/empty/loading ── */
  .status-bar { text-align: center; margin-bottom: 40px; padding: 16px 24px; background: white; border-radius: 12px; box-shadow: 0 2px 12px var(--shadow); font-size: 14px; color: #6B5744; }
  .status-bar strong { color: var(--bark); }
  .empty { text-align: center; padding: 80px 24px; color: #6B5744; }
  .empty h2 { font-family: 'Playfair Display', serif; font-size: 1.8rem; margin-bottom: 12px; }
  .loading { text-align: center; padding: 80px 24px; color: #6B5744; font-size: 15px; }
  .error-msg { background: #FFF0ED; border: 1px solid #FFCCC0; border-radius: 8px; padding: 12px 16px; font-size: 13px; color: var(--rust); margin-top: 12px; text-align: left; }
  .success-banner { background: #EFF7F0; border: 1px solid #C0DFC4; border-radius: 12px; padding: 20px 28px; text-align: center; margin-bottom: 32px; font-size: 15px; color: #3A6B3E; }
  .success-banner strong { display: block; font-family: 'Playfair Display', serif; font-size: 1.2rem; margin-bottom: 4px; }
`;

// ── HELPERS ───────────────────────────────────────────────────────────────────
function parseBullets(notes) {
  if (!notes) return [];
  return notes.split("\n").map(b => b.replace(/^•\s*/, "").trim()).filter(Boolean);
}

function isOddWeek() {
  const now = new Date();
  const startOfYear = new Date(now.getFullYear(), 0, 1);
  const days_diff = (now - startOfYear) / 86400000;
  const jan1_js_day = (startOfYear.getDay() + 1) % 7;
  const weekNum = Math.ceil((days_diff + jan1_js_day + 1) / 7);
  return weekNum % 2 !== 0;
}

function priceLabel(level) {
  const map = { "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$", "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$" };
  return map[level] || level || "";
}

// ── PET TILE ──────────────────────────────────────────────────────────────────
function PetTile({ pet, onApprove, approving, approved }) {
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
          <button className="btn btn-approve" onClick={() => onApprove(pet)} disabled={approving === pet.source_url}>
            {approving === pet.source_url ? "Approving..." : "Approve this pet"}
          </button>
        )}
      </div>
    </div>
  );
}

// ── RESTAURANT TILE ───────────────────────────────────────────────────────────
function RestaurantTile({ restaurant, onApprove, approving, approved }) {
  const localStatus = restaurant._localStatus;
  const bullets     = parseBullets(restaurant.scoring_notes);
  const total       = restaurant.total_score ? parseInt(restaurant.total_score) : null;
  const rating      = parseFloat(restaurant.rating) || 0;
  const price       = priceLabel(restaurant.price_level);

  return (
    <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      <div className="tile-photo">
        {restaurant.photo_url ? <img src={restaurant.photo_url} alt={restaurant.restaurant_name} /> : <span>No photo available</span>}
      </div>
      <div className="tile-body">
        <div className="tile-meta">
          {restaurant.cuisine_type && <span className="tile-cuisine">{restaurant.cuisine_type}</span>}
          {rating > 0 && (
            <span className="tile-rating">
              <span style={{color: "#F4A523"}}>{"★".repeat(Math.floor(rating))}{"☆".repeat(5 - Math.floor(rating))}</span>
              &nbsp;{rating} ({parseInt(restaurant.review_count || 0).toLocaleString()})
            </span>
          )}
          {price && <span className="tile-price">{price}</span>}
        </div>
        <div className="tile-name">{restaurant.restaurant_name}</div>
        {total !== null && (
          <div className="score-bar">
            <div className="score-total">{total}<span>/40</span></div>
            <div className="score-pills">
              {restaurant.appeal_score           && <span className="score-pill">✨ Appeal {restaurant.appeal_score}</span>}
              {restaurant.uniqueness_score       && <span className="score-pill">🌟 Unique {restaurant.uniqueness_score}</span>}
              {restaurant.neighborhood_fit_score && <span className="score-pill">🏘 Fit {restaurant.neighborhood_fit_score}</span>}
              {restaurant.festive_score          && <span className="score-pill">🎉 Festive {restaurant.festive_score}</span>}
            </div>
          </div>
        )}
        {bullets.length > 0 && (
          <div className="scoring-notes">
            <div className="scoring-notes-label">Why feature this restaurant</div>
            <ul>{bullets.map((b, i) => <li key={i}>{b}</li>)}</ul>
          </div>
        )}
        <div className="tile-blurb">{restaurant.blurb}</div>
        <div className="tile-info">
          {restaurant.address && <div>{restaurant.address}</div>}
          {restaurant.phone   && <div>{restaurant.phone}</div>}
          {restaurant.hours   && <div style={{marginTop: 4, fontSize: 11}}>{restaurant.hours}</div>}
          {restaurant.website_url && <a className="tile-link" href={restaurant.website_url} target="_blank" rel="noreferrer">Visit website →</a>}
        </div>
        {restaurant.google_maps_url && (
          <a className="btn-maps" href={restaurant.google_maps_url} target="_blank" rel="noreferrer">
            📍 View on Google Maps
          </a>
        )}
        {!approved && (
          <button className="btn btn-approve" onClick={() => onApprove(restaurant)} disabled={approving === restaurant.place_id}>
            {approving === restaurant.place_id ? "Approving..." : "Approve this restaurant"}
          </button>
        )}
      </div>
    </div>
  );
}

// ── PETS PAGE ─────────────────────────────────────────────────────────────────
function PetsPage({ token }) {
  const [pets, setPets]                     = useState([]);
  const [newsletters, setNewsletters]       = useState([]);
  const [selectedNewsletter, setNewsletter] = useState("");
  const [loading, setLoading]               = useState(false);
  const [approving, setApproving]           = useState(null);
  const [approved, setApproved]             = useState(null);
  const [error, setError]                   = useState("");
  const [success, setSuccess]               = useState("");

  const DATA_URL = "/NewsletterAutomation/pets.json";

  useEffect(() => { fetchPets(); }, []);

  async function fetchPets() {
    setLoading(true);
    setError("");
    try {
      const res     = await fetch(DATA_URL);
      const pets    = await res.json();
      const pending = pets.filter(r => r.status === "pending");
      const names   = [...new Set(pending.map(r => r.newsletter_name).filter(Boolean))];
      setNewsletters(names);
      if (names.length > 0) setNewsletter(prev => prev || names[0]);
      setPets(pending);
    } catch (e) {
      setError("Could not load pets data.");
    } finally {
      setLoading(false);
    }
  }

  async function handleApprove(pet) {
    if (!token) return;
    setApproving(pet.source_url);
    setError("");
    try {
      const res = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW_PETS}/dispatches`,
        { method: "POST", headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: { source_url: pet.source_url } }) }
      );
      if (!res.ok) { const err = await res.json(); throw new Error(err.message || "GitHub API error"); }
      setApproved(pet.source_url);
      setSuccess(`${pet.pet_name} approved!`);
      setPets(prev => prev.map(p => ({ ...p, _localStatus: p.source_url === pet.source_url ? "approved" : "rejected" })));
    } catch (e) {
      setError(`Approval failed: ${e.message}`);
    } finally {
      setApproving(null);
    }
  }

  const oddWeek       = isOddWeek();
  const weekType      = oddWeek ? "cat" : "dog";
  const visiblePets   = pets.filter(p => p.newsletter_name === selectedNewsletter);
  const overallWinner = visiblePets.find(p => p.default_winner === "yes");
  const catWinner     = visiblePets.find(p => p.cat_default === "yes");
  const dogWinner     = visiblePets.find(p => p.dog_default === "yes");
  const candidates    = visiblePets.filter(p => (p.animal_type || "").toLowerCase() === weekType);

  if (loading) return <div className="loading">Loading this week's candidates...</div>;
  if (pets.length === 0) return <div className="empty"><h2>All clear!</h2><p>No pending pets found. Run the pipeline to generate new candidates.</p></div>;

  return (
    <>
      {success && <div className="success-banner"><strong>Approved!</strong>{success}</div>}
      {error   && <div className="error-msg" style={{marginBottom: 24}}>{error}</div>}

      {newsletters.length > 0 && (
        <div style={{marginBottom: 32, textAlign: "center"}}>
          <select className="newsletter-select" value={selectedNewsletter} onChange={e => setNewsletter(e.target.value)}>
            {newsletters.map(n => <option key={n} value={n}>{n.replace(/_/g, " ")}</option>)}
          </select>
        </div>
      )}

      {/* Default Winners */}
      <div style={{marginBottom: 32}}>
        <div className="default-winners-label" style={{
          fontFamily: "'DM Sans', sans-serif",
          fontWeight: 500,
          fontSize: 11,
          letterSpacing: "0.2em",
          textTransform: "uppercase",
          color: "var(--rust)",
          marginBottom: 24
        }}>
          Default Winners — {oddWeek ? "Odd Week (Cat Week)" : "Even Week (Dog Week)"}
        </div>
      
        <div className="tiles">
          {[
            { label: "Overall", pet: overallWinner },
            { label: "Cat",     pet: catWinner },
            { label: "Dog",     pet: dogWinner },
          ].filter(w => w.pet).map(({ label, pet }) => (
            <div key={label} style={{position: "relative"}}>
              <div style={{
                position: "absolute",
                top: 16,
                left: 16,
                zIndex: 3,
                background: label === "Overall" ? "var(--rust)" : "var(--sage)",
                color: "white",
                borderRadius: 99,
                padding: "3px 12px",
                fontSize: 11,
                fontWeight: 500,
                letterSpacing: "0.1em",
                textTransform: "uppercase"
              }}>
                {label} Default
              </div>
              <PetTile
                pet={pet}
                onApprove={handleApprove}
                approving={approving}
                approved={approved}
              />
            </div>
          ))}
        </div>
      </div>
      
      <hr className="divider" />

      <div className="status-bar">
        <strong>{candidates.length}</strong> {weekType} candidates this week &mdash; select one to feature
      </div>

      {candidates.length === 0 ? (
        <div className="empty"><h2>No {weekType} candidates</h2><p>Run the pipeline to generate new candidates.</p></div>
      ) : (
        <div className="tiles">
          {candidates.map((pet, idx) => (
            <PetTile key={pet.source_url || idx} pet={pet} onApprove={handleApprove} approving={approving} approved={approved} />
          ))}
        </div>
      )}
    </>
  );
}

// ── RESTAURANTS PAGE ──────────────────────────────────────────────────────────
function RestaurantsPage({ token }) {
  const [restaurants, setRestaurants]       = useState([]);
  const [newsletters, setNewsletters]       = useState([]);
  const [selectedNewsletter, setNewsletter] = useState("");
  const [loading, setLoading]               = useState(false);
  const [approving, setApproving]           = useState(null);
  const [approved, setApproved]             = useState(null);
  const [error, setError]                   = useState("");
  const [success, setSuccess]               = useState("");

  const DATA_URL = "/NewsletterAutomation/restaurants.json";
  
  useEffect(() => { fetchRestaurants(); }, []);
  
  async function fetchRestaurants() {
    setLoading(true);
    setError("");
    try {
      const res         = await fetch(DATA_URL);
      const restaurants = await res.json();
      const pending     = restaurants.filter(r => r.status === "pending");
      const names       = [...new Set(pending.map(r => r.newsletter_name).filter(Boolean))];
      setNewsletters(names);
      if (names.length > 0) setNewsletter(prev => prev || names[0]);
      setRestaurants(pending);
    } catch (e) {
      setError("Could not load restaurants data.");
    } finally {
      setLoading(false);
    }
  }

  async function handleApprove(restaurant) {
    if (!token) return;
    setApproving(restaurant.place_id);
    setError("");
    try {
      const res = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW_REST}/dispatches`,
        { method: "POST", headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: { place_id: restaurant.place_id } }) }
      );
      if (!res.ok) { const err = await res.json(); throw new Error(err.message || "GitHub API error"); }
      setApproved(restaurant.place_id);
      setSuccess(`${restaurant.restaurant_name} approved!`);
      setRestaurants(prev => prev.map(r => ({ ...r, _localStatus: r.place_id === restaurant.place_id ? "approved" : "rejected" })));
    } catch (e) {
      setError(`Approval failed: ${e.message}`);
    } finally {
      setApproving(null);
    }
  }

  const visibleRest   = restaurants.filter(r => r.newsletter_name === selectedNewsletter);
  const defaultWinner = visibleRest.find(r => r.default_winner === "yes");

  if (loading) return <div className="loading">Loading this week's restaurant candidates...</div>;
  if (restaurants.length === 0) return <div className="empty"><h2>All clear!</h2><p>No pending restaurants found. Run the pipeline to generate new candidates.</p></div>;

  return (
    <>
      {success && <div className="success-banner"><strong>Approved!</strong>{success}</div>}
      {error   && <div className="error-msg" style={{marginBottom: 24}}>{error}</div>}

      {newsletters.length > 0 && (
        <div style={{marginBottom: 32, textAlign: "center"}}>
          <select className="newsletter-select" value={selectedNewsletter} onChange={e => setNewsletter(e.target.value)}>
            {newsletters.map(n => <option key={n} value={n}>{n.replace(/_/g, " ")}</option>)}
          </select>
        </div>
      )}

      <div className="default-winners">
        <div className="default-winners-label">Default Winner</div>
        <div className="default-winners-rows">
          <div className="default-winner-row">
            <span className="winner-badge winner-badge-rest">Restaurant</span>
            <span className="winner-name">{defaultWinner ? defaultWinner.restaurant_name : "None set"}</span>
            {defaultWinner && <span className="winner-score">{defaultWinner.total_score}/40</span>}
          </div>
        </div>
      </div>

      <hr className="divider" />

      <div className="status-bar">
        <strong>{visibleRest.length}</strong> restaurant candidates this week &mdash; select one to feature
      </div>

      {visibleRest.length === 0 ? (
        <div className="empty"><h2>No candidates</h2><p>Run the pipeline to generate new restaurant candidates.</p></div>
      ) : (
        <div className="tiles">
          {visibleRest.map((r, idx) => (
            <RestaurantTile key={r.place_id || idx} restaurant={r} onApprove={handleApprove} approving={approving} approved={approved} />
          ))}
        </div>
      )}
    </>
  );
}

// ── MAIN APP ──────────────────────────────────────────────────────────────────
export default function App() {
  const [token, setToken]           = useState(() => localStorage.getItem("gh_token") || "");
  const [tokenInput, setTokenInput] = useState("");
  const [error, setError]           = useState("");
  const [activePage, setActivePage] = useState("pets");

  const isAuthed = Boolean(token);

  const [step, setStep] = useState("password"); // "password" or "token"
  
  function handleTokenSubmit() {
    if (step === "password") {
      if (tokenInput.trim() === APP_PASSWORD) {
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

  const pages = [
    { id: "pets",        label: "🐾 Pets" },
    { id: "restaurants", label: "🍽 Restaurants" },
  ];

  const pageHeaders = {
    pets:        { eyebrow: "Newsletter Pet Review",        h1: <>Pick This Week's<br/><em>Featured Friend</em></>,        sub: "Review candidates and approve the one that best fits the newsletter." },
    restaurants: { eyebrow: "Newsletter Restaurant Review", h1: <>Pick This Week's<br/><em>Featured Restaurant</em></>, sub: "Review candidates and approve the one that best fits the newsletter." },
  };

  const currentHeader = pageHeaders[activePage];

  return (
    <>
      <style>{styles}</style>
      <div className="app">
        {!isAuthed ? (
          <>
            <div className="header">
              <p className="header-eyebrow">Newsletter Review</p>
              <h1>Pick This Week's<br/><em>Best Content</em></h1>
              <p className="header-sub">Review and approve pets and restaurants for your newsletters.</p>
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
          <>
            {/* Responsive nav -- horizontal tabs on desktop, dropdown on mobile */}
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

            {/* Page header */}
            <div className="header">
              <p className="header-eyebrow">{currentHeader.eyebrow}</p>
              <h1>{currentHeader.h1}</h1>
              <p className="header-sub">{currentHeader.sub}</p>
            </div>

            {/* Page content */}
            {activePage === "pets"        && <PetsPage        token={token} />}
            {activePage === "restaurants" && <RestaurantsPage token={token} />}
          </>
        )}
      </div>
    </>
  );
}
