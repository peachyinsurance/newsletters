export function parseBullets(notes) {
  if (!notes) return [];
  return notes.split("\n").map(b => b.replace(/^•\s*/, "").trim()).filter(Boolean);
}

export function isOddWeek() {
  const now = new Date();
  const startOfYear = new Date(now.getFullYear(), 0, 1);
  const days_diff = (now - startOfYear) / 86400000;
  const jan1_js_day = (startOfYear.getDay() + 1) % 7;
  const weekNum = Math.ceil((days_diff + jan1_js_day + 1) / 7);
  return weekNum % 2 !== 0;
}

export function priceLabel(level) {
  const map = {
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
  };
  return map[level] || level || "";
}

// Password is hashed to avoid exposing it in plain text in the JS bundle.
const PASSWORD_HASH = "5e5c311627e41287f3a83333a8d1706bdb81e6393463fe6a1133eb57a2950425";

export async function checkPassword(input) {
  const encoder = new TextEncoder();
  const data = encoder.encode(input);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  const hashHex = hashArray.map(b => b.toString(16).padStart(2, "0")).join("");
  return hashHex === PASSWORD_HASH;
}
