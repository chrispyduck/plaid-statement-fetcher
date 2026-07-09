function stringifyDetails(details) {
  if (!details) return '';
  if (typeof details === 'string') return details;
  if (typeof details === 'object') {
    const parts = [];
    if (details.message) parts.push(`message=${details.message}`);
    if (details.error_code) parts.push(`error_code=${details.error_code}`);
    if (details.error_type) parts.push(`error_type=${details.error_type}`);
    if (details.request_id) parts.push(`request_id=${details.request_id}`);
    if (details.documentation_url) parts.push(`docs=${details.documentation_url}`);
    if (parts.length) return parts.join(' | ');
    return JSON.stringify(details);
  }
  return String(details);
}

function normalizeBaseUrl(value) {
  if (!value) return '';
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

export function apiBaseUrl() {
  return normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL || '');
}

export function plaidOriginUrl() {
  if (import.meta.env.VITE_PLAID_ORIGIN) {
    return import.meta.env.VITE_PLAID_ORIGIN;
  }
  if (window.location.port === '5173') {
    return 'https://statement-fetcher.localhost:8765';
  }
  return window.location.origin;
}

export async function parseApiError(response) {
  const fallback = `${response.status} ${response.statusText}`;
  try {
    const payload = await response.json();
    return stringifyDetails(payload.detail || payload) || fallback;
  } catch (_jsonError) {
    try {
      const text = await response.text();
      return text || fallback;
    } catch (_textError) {
      return fallback;
    }
  }
}

export async function fetchJson(path, options = {}) {
  const response = await fetch(`${apiBaseUrl()}${path}`, options);
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
  return response.json();
}

export function statementDownloadUrl(dedupeKey) {
  return `${apiBaseUrl()}/api/statements/${encodeURIComponent(String(dedupeKey || ''))}/download`;
}
