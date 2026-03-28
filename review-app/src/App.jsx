import { useState, useEffect } from "react";

// ── CONFIG ────────────────────────────────────────────────────────────────────
const GITHUB_OWNER    = "couch2coders";
const GITHUB_REPO     = "NewsletterAutomation";
const GITHUB_WORKFLOW = "approve_pet.yml";
const GSHEET_ID       = "1EDEvBSWA0sTiLJBv4p36E5-bg1YHSGi04DTQWCbEc4c";
const GSHEET_TAB      = "Pets";
const APP_PASSWORD    = "Adm1n$$";
const GITHUB_TOKEN    = import.meta.env.VITE_GITHUB_TOKEN;

// ── STYLES ────────────────────────────────────────────────────────────────────
const [newsletters, setNewsletters]         = useState([]);
const [selectedNewsletter, setNewsletter]   = useState("");
const styles = `
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;1,400&family=DM+Sans:wght@300;400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --cream:   #F7F3EE;
    --bark:    #2C1A0E;
    --rust:    #C4531A;
    --sage:    #7A9E7E;
    --sand:    #E8DDD0;
    --shadow:  rgba(44,26,14,0.12);
  }

  body {
    background: var(--cream);
    font-family: 'DM Sans', sans-serif;
    color: var(--bark);
    min-height: 100vh;
  }

  .app { max-width: 1200px; margin: 0 auto; padding: 48px 24px; }

  .header { text-align: center; margin-bottom: 56px; }
  .header-eyebrow {
    font-family: 'DM Sans', sans-serif;
    font-weight: 300;
    font-size: 11px;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    color: var(--rust);
    margin-bottom: 12px;
  }
  .header h1 {
    font-family: 'Playfair Display', serif;
    font-size: clamp(2rem, 5vw, 3.5rem);
    font-weight: 700;
    line-height: 1.1;
    color: var(--bark);
  }
  .header h1 em { font-style: italic; color: var(--rust); }
  .header-sub {
    margin-top: 16px;
    font-size: 15px;
    font-weight: 300;
    color: #6B5744;
    max-width: 480px;
    margin-left: auto;
    margin-right: auto;
    line-height: 1.6;
  }

  .token-gate {
    max-width: 480px;
    margin: 0 auto;
    background: white;
    border-radius: 16px;
    padding: 40px;
    box-shadow: 0 4px 32px var(--shadow);
    text-align: center;
  }
  .token-gate h2 { font-family: 'Playfair Display', serif; font-size: 1.5rem; margin-bottom: 8px; }
  .token-gate p { font-size: 14px; color: #6B5744; margin-bottom: 24px; line-height: 1.6; }

  .token-input {
    width: 100%;
    padding: 12px 16px;
    border: 1.5px solid var(--sand);
    border-radius: 8px;
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    background: var(--cream);
    color: var(--bark);
    margin-bottom: 12px;
    outline: none;
    transition: border-color 0.2s;
  }
  .token-input:focus { border-color: var(--rust); }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 12px 28px;
    border-radius: 8px;
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: all 0.2s;
  }
  .btn-primary { background: var(--rust); color: white; width: 100%; justify-content: center; }
  .btn-primary:hover { background: #A8441A; transform: translateY(-1px); }
  .btn-primary:disabled { background: #C4A090; cursor: not-allowed; transform: none; }

  .btn-approve {
    background: var(--sage);
    color: white;
    width: 100%;
    justify-content: center;
    margin-top: 20px;
    padding: 14px 28px;
    font-size: 15px;
  }
  .btn-approve:hover { background: #5F8563; transform: translateY(-1px); }
  .btn-approve:disabled { background: #A8C4AA; cursor: not-allowed; transform: none; }

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 32px;
  }

  .tile {
    background: white;
    border-radius: 20px;
    overflow: hidden;
    box-shadow: 0 4px 24px var(--shadow);
    transition: transform 0.25s, box-shadow 0.25s;
    display: flex;
    flex-direction: column;
    position: relative;
  }
  .tile:hover { transform: translateY(-4px); box-shadow: 0 12px 40px var(--shadow); }
  .tile.approved { outline: 3px solid var(--sage); outline-offset: -3px; }
  .tile.rejected { opacity: 0.4; pointer-events: none; }

  .tile-badge {
    position: absolute;
    top: 16px;
    right: 16px;
    background: var(--sage);
    color: white;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding: 4px 12px;
    border-radius: 99px;
    z-index: 2;
  }

  .tile-photo {
    width: 100%;
    height: 240px;
    background: var(--sand);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #A89080;
    font-size: 13px;
    flex-shrink: 0;
  }
  .tile-photo img { width: 100%; height: 100%; object-fit: cover; }

  .tile-body { padding: 28px; flex: 1; display: flex; flex-direction: column; }

  .tile-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .tile-shelter {
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--rust);
  }

  .tile-name {
    font-family: 'Playfair Display', serif;
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--bark);
    margin-bottom: 16px;
    line-height: 1.2;
  }

  .score-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    padding: 12px 16px;
    background: var(--cream);
    border-radius: 10px;
  }
  .score-total {
    font-family: 'Playfair Display', serif;
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--rust);
    white-space: nowrap;
  }
  .score-total span {
    font-size: 0.8rem;
    color: #A89080;
    font-family: 'DM Sans', sans-serif;
    font-weight: 300;
  }
  .score-pills { display: flex; flex-wrap: wrap; gap: 6px; }
  .score-pill {
    font-size: 11px;
    font-weight: 500;
    padding: 3px 8px;
    border-radius: 99px;
    background: white;
    border: 1px solid var(--sand);
    color: #6B5744;
    white-space: nowrap;
  }

  .scoring-notes {
    margin-bottom: 16px;
    padding: 14px 16px;
    background: #F0F7F1;
    border-radius: 10px;
    border-left: 3px solid var(--sage);
  }
  .scoring-notes-label {
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--sage);
    margin-bottom: 8px;
  }
  .scoring-notes ul { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 6px; }
  .scoring-notes li { font-size: 13px; line-height: 1.5; color: #3A5C3E; }

  .tile-blurb {
    font-size: 14px;
    line-height: 1.75;
    color: #4A3728;
    font-weight: 300;
    flex: 1;
    white-space: pre-wrap;
  }

  .tile-shelter-info {
    margin-top: 20px;
    padding-top: 20px;
    border-top: 1px solid var(--sand);
    font-size: 12px;
    color: #6B5744;
    line-height: 1.8;
  }
  .tile-link { display: inline-block; margin-top: 8px; font-size: 12px; color: var(--rust); text-decoration: none; font-weight: 500; }
  .tile-link:hover { text-decoration: underline; }

  .status-bar {
    text-align: center;
    margin-bottom: 40px;
    padding: 16px 24px;
    background: white;
    border-radius: 12px;
    box-shadow: 0 2px 12px var(--shadow);
    font-size: 14px;
    color: #6B5744;
  }
  .status-bar strong { color: var(--bark); }

  .empty { text-align: center; padding: 80px 24px; color: #6B5744; }
  .empty h2 { font-family: 'Playfair Display', serif; font-size: 1.8rem; margin-bottom: 12px; }

  .loading { text-align: center; padding: 80px 24px; color: #6B5744; font-size: 15px; }

  .error-msg {
    background: #FFF0ED;
    border: 1px solid #FFCCC0;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 13px;
    color: var(--rust);
    margin-top: 12px;
    text-align: left;
  }

  .success-banner {
    background: #EFF7F0;
    border: 1px solid #C0DFC4;
    border-radius: 12px;
    padding: 20px 28px;
    text-align: center;
    margin-bottom: 32px;
    font-size: 15px;
    color: #3A6B3E;
  }
  .success-banner strong {
    display: block;
    font-family: 'Playfair Display', serif;
    font-size: 1.2rem;
    margin-bottom: 4px;
  }
`;

// ── HELPERS ───────────────────────────────────────────────────────────────────
function parseCSV(text) {
  const cleaned = text.replace(/\r/g, "");  // add this line
  const rows = [];
  let cur = "", inQ = false;
  for (let i = 0; i < cleaned.length; i++) {  // use cleaned instead of text

function parseBullets(notes) {
  if (!notes) return [];
  return notes
    .split("\n")
    .map(b => b.replace(/^•\s*/, "").trim())
    .filter(Boolean);
}

// ── MAIN APP ──────────────────────────────────────────────────────────────────
export default function PetReviewApp() {
  const [token, setToken]           = useState(() => localStorage.getItem("gh_token") || "");
  const [tokenInput, setTokenInput] = useState("");
  const [pets, setPets]             = useState([]);
  const [loading, setLoading]       = useState(false);
  const [approving, setApproving]   = useState(null);
  const [approved, setApproved]     = useState(null);
  const [error, setError]           = useState("");
  const [success, setSuccess]       = useState("");

  const isAuthed  = Boolean(token);
  const SHEET_CSV = `https://docs.google.com/spreadsheets/d/${GSHEET_ID}/export?format=csv&sheet=${encodeURIComponent(GSHEET_TAB)}`;
  useEffect(() => {
    if (!isAuthed) return;
    fetchPets();
  }, [isAuthed]);

  async function fetchPets() {
    setLoading(true);
    setError("");
    try {
      const res  = await fetch(SHEET_CSV);
      const text = await res.text();
      const rows = parseCSV(text);
      const pending = rows.filter(r => r.status === "pending");
  
      // Extract unique newsletter names
      const names = [...new Set(pending.map(r => r.newsletter_name).filter(Boolean))];
      setNewsletters(names);
      if (!selectedNewsletter && names.length > 0) {
        setNewsletter(names[0]);
      }
  
      setPets(pending);
    } catch (e) {
      setError("Could not load pets from Google Sheets.");
    } finally {
      setLoading(false);
    }
  }


  function isOddWeek() {
  const now = new Date();
  const startOfYear = new Date(now.getFullYear(), 0, 1);
  const weekNum = Math.ceil(((now - startOfYear) / 86400000 + startOfYear.getDay() + 1) / 7);
  return weekNum % 2 !== 0;
  }
  
  async function handleApprove(pet) {
    if (!token) return;
    setApproving(pet.source_url);
    setError("");
    try {
      const res = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            Accept: "application/vnd.github+json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ ref: "main", inputs: { source_url: pet.source_url } })
        }
      );
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.message || "GitHub API error");
      }
      setApproved(pet.source_url);
      setSuccess(`${pet.pet_name} approved!`);
      setPets(prev => prev.map(p => ({
        ...p,
        _localStatus: p.source_url === pet.source_url ? "approved" : "rejected"
      })));
    } catch (e) {
      setError(`Approval failed: ${e.message}`);
    } finally {
      setApproving(null);
    }
  }

  function handleTokenSubmit() {
  if (tokenInput.trim() === APP_PASSWORD) {
    localStorage.setItem("gh_token", GITHUB_TOKEN);
    setToken(GITHUB_TOKEN);
    setTokenInput("");
  } else {
    setError("Incorrect password.");
  }
}

    // ── RENDER ────────────────────────────────────────────────────────────────
  return (
    <>
      <style>{styles}</style>
      <div className="app">
        <div className="header">
          <p className="header-eyebrow">East Cobb Connect</p>
          <h1>Pick This Week's<br/><em>Featured Friend</em></h1>
          <p className="header-sub">
            Review the three candidates and approve the one that best fits the newsletter.
          </p>
        </div>

        {!isAuthed ? (
          <div className="token-gate">
            <h2>Sign In</h2>
            <p>Enter your GitHub Personal Access Token to load this week's pets and approve a blurb.</p>
            <input
              className="token-input"
              type="password"
              placeholder="Enter password"
              value={tokenInput}
              onChange={e => setTokenInput(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleTokenSubmit()}
            />
            <button className="btn btn-primary" onClick={handleTokenSubmit}>Continue</button>
            {error && <div className="error-msg">{error}</div>}
          </div>

        ) : loading ? (
          <div className="loading">Loading this week's candidates...</div>

        ) : pets.length === 0 ? (
          <div className="empty">
            <h2>All clear!</h2>
            <p>No pending pets found. Run the pipeline to generate new candidates.</p>
          </div>

        ) : (
          <>
            {success && (
              <div className="success-banner">
                <strong>Approved!</strong>
                {success}
              </div>
            )}
            {error && <div className="error-msg" style={{marginBottom: 24}}>{error}</div>}

            {/* Newsletter dropdown */}
            {newsletters.length > 1 && (
              <div style={{marginBottom: 32, textAlign: "center"}}>
                <select
                  value={selectedNewsletter}
                  onChange={e => setNewsletter(e.target.value)}
                  style={{
                    padding: "10px 20px",
                    borderRadius: 8,
                    border: "1.5px solid var(--sand)",
                    fontFamily: "'DM Sans', sans-serif",
                    fontSize: 15,
                    background: "white",
                    color: "var(--bark)",
                    cursor: "pointer"
                  }}
                >
                  {newsletters.map(n => (
                    <option key={n} value={n}>{n.replace(/_/g, " ")}</option>
                  ))}
                </select>
              </div>
            )}

            {/* Default Winners + Candidates */}
            {(() => {
              const visiblePets   = pets.filter(p => p.newsletter_name === selectedNewsletter);
              const overallWinner = visiblePets.find(p => p.default_winner === "yes");
              const catWinner     = visiblePets.find(p => p.cat_default === "yes");
              const dogWinner     = visiblePets.find(p => p.dog_default === "yes");
              const oddWeek       = isOddWeek();
              const weekType      = oddWeek ? "cat" : "dog";
              const candidates    = visiblePets.filter(p => p.animal_type === weekType);

              return (
                <>
                  {/* Default Winners Box */}
                  <div style={{
                    background: "white",
                    borderRadius: 16,
                    padding: "24px 28px",
                    marginBottom: 32,
                    boxShadow: "0 4px 24px var(--shadow)"
                  }}>
                    <div style={{
                      fontFamily: "'DM Sans', sans-serif",
                      fontWeight: 500,
                      fontSize: 11,
                      letterSpacing: "0.2em",
                      textTransform: "uppercase",
                      color: "var(--rust)",
                      marginBottom: 16
                    }}>
                      Default Winners — {oddWeek ? "Odd Week • Cat Week" : "Even Week • Dog Week"}
                    </div>

                    <div style={{display: "flex", flexDirection: "column", gap: 10}}>
                      <div style={{display: "flex", alignItems: "center", gap: 12}}>
                        <span style={{background: "var(--rust)", color: "white", borderRadius: 99, padding: "2px 10px", fontSize: 11, fontWeight: 500}}>Overall</span>
                        <span style={{fontSize: 15, fontWeight: 500}}>
                          {overallWinner ? `${overallWinner.pet_name} (${overallWinner.animal_type})` : "None set"}
                        </span>
                        {overallWinner && <span style={{fontSize: 13, color: "#6B5744"}}>{overallWinner.total_score}/30</span>}
                      </div>

                      <div style={{display: "flex", alignItems: "center", gap: 12}}>
                        <span style={{background: "#7A9E7E", color: "white", borderRadius: 99, padding: "2px 10px", fontSize: 11, fontWeight: 500}}>Cat</span>
                        <span style={{fontSize: 15, fontWeight: 500}}>
                          {catWinner ? catWinner.pet_name : "None set"}
                        </span>
                        {catWinner && <span style={{fontSize: 13, color: "#6B5744"}}>{catWinner.total_score}/30</span>}
                      </div>

                      <div style={{display: "flex", alignItems: "center", gap: 12}}>
                        <span style={{background: "#7A9E7E", color: "white", borderRadius: 99, padding: "2px 10px", fontSize: 11, fontWeight: 500}}>Dog</span>
                        <span style={{fontSize: 15, fontWeight: 500}}>
                          {dogWinner ? dogWinner.pet_name : "None set"}
                        </span>
                        {dogWinner && <span style={{fontSize: 13, color: "#6B5744"}}>{dogWinner.total_score}/30</span>}
                      </div>
                    </div>
                  </div>

                  {/* Separator */}
                  <hr style={{border: "none", borderTop: "1px solid var(--sand)", marginBottom: 32}} />

                  {/* Candidates */}
                  <div className="status-bar">
                    <strong>{candidates.length}</strong> {weekType} candidates this week &mdash; select one to feature
                  </div>

                  <div className="tiles">
                    {candidates.map((pet, idx) => {
                      const localStatus = pet._localStatus;
                      const bullets     = parseBullets(pet.scoring_notes);
                      const total       = pet.total_score ? parseInt(pet.total_score) : null;

                      return (
                        <div
                          key={pet.source_url || idx}
                          className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}
                        >
                          {localStatus === "approved" && (
                            <div className="tile-badge">✓ Approved</div>
                          )}

                          <div className="tile-photo">
                            {pet.photo_url
                              ? <img src={pet.photo_url} alt={pet.pet_name} />
                              : <span>No photo available</span>
                            }
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
                                <ul>
                                  {bullets.map((b, i) => <li key={i}>{b}</li>)}
                                </ul>
                              </div>
                            )}

                            <div className="tile-blurb">{pet.blurb}</div>

                            <div className="tile-shelter-info">
                              {pet.shelter_address && <div>{pet.shelter_address}</div>}
                              {pet.shelter_phone   && <div>{pet.shelter_phone}{pet.shelter_email ? ` | ${pet.shelter_email}` : ""}</div>}
                              {pet.shelter_hours   && <div>{pet.shelter_hours}</div>}
                              {pet.source_url      && (
                                <a className="tile-link" href={pet.source_url} target="_blank" rel="noreferrer">
                                  View listing →
                                </a>
                              )}
                            </div>

                            {!approved && (
                              <button
                                className="btn btn-approve"
                                onClick={() => handleApprove(pet)}
                                disabled={approving === pet.source_url}
                              >
                                {approving === pet.source_url ? "Approving..." : "Approve this pet"}
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </>
              );
            })()}
          </>
        )}
      </div>
    </>
  );
}
