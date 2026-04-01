import { parseBullets, priceLabel } from "./helpers";

export default function RestaurantTile({ restaurant, onApprove, approving, approved }) {
  const localStatus = restaurant._localStatus;
  const bullets     = parseBullets(restaurant.scoring_notes);
  const total       = restaurant.total_score ? parseInt(restaurant.total_score) : null;

  return (
    <div className={`tile ${localStatus === "approved" ? "approved" : localStatus === "rejected" ? "rejected" : ""}`}>
      {localStatus === "approved" && <div className="tile-badge">✓ Approved</div>}
      <div className="tile-photo">
        {restaurant.photo_url ? <img src={restaurant.photo_url} alt={restaurant.restaurant_name} /> : <span>No photo available</span>}
      </div>
      <div className="tile-body">
        <div className="tile-meta">
          {restaurant.cuisine_type && <span className="tile-cuisine">{restaurant.cuisine_type}</span>}
          {restaurant.rating && (
            <span className="tile-rating">⭐ {restaurant.rating} ({restaurant.review_count} reviews)</span>
          )}
          {restaurant.price_level && <span className="tile-price">{priceLabel(restaurant.price_level)}</span>}
        </div>
        <div className="tile-name">{restaurant.restaurant_name}</div>
        {total !== null && (
          <div className="score-bar">
            <div className="score-total">{total}<span>/40</span></div>
            <div className="score-pills">
              {restaurant.appeal_score && <span className="score-pill">🌟 Appeal {restaurant.appeal_score}</span>}
              {restaurant.uniqueness_score && <span className="score-pill">✨ Unique {restaurant.uniqueness_score}</span>}
              {restaurant.neighborhood_fit_score && <span className="score-pill">📍 Fit {restaurant.neighborhood_fit_score}</span>}
              {restaurant.festive_score && <span className="score-pill">🎉 Festive {restaurant.festive_score}</span>}
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
          {restaurant.phone && <div>{restaurant.phone}</div>}
          {restaurant.hours && <div>{restaurant.hours}</div>}
          {restaurant.website_url && <a className="tile-link" href={restaurant.website_url} target="_blank" rel="noreferrer">Visit website →</a>}
        </div>
        {restaurant.google_maps_url && (
          <a className="btn btn-maps" href={restaurant.google_maps_url} target="_blank" rel="noreferrer">
            📍 View on Google Maps
          </a>
        )}
        {!approved && (
          <button className="btn btn-approve" onClick={() => onApprove(restaurant)} disabled={!!approving}>
            {approving === restaurant.place_id ? "Approving..." : "Approve this restaurant"}
          </button>
        )}
      </div>
    </div>
  );
}
