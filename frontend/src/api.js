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

/**
 * Re-authenticates an existing linked item via Plaid Link's update mode, then
 * confirms the reconnect by refreshing one of its accounts (which clears the
 * reauth-needed flag once Plaid accepts the stored access token again).
 *
 * Deliberately does NOT call /api/plaid/link/exchange: update mode re-authenticates
 * the item in place and keeps the same item_id/access_token, so exchanging a public
 * token here would be wrong -- that path is only for linking a brand-new item, and
 * using it for a reconnect is what created duplicate accounts for the same
 * institution before this helper existed.
 *
 * Resolves with `{ cancelled: true }` if the user closes Link without finishing, or
 * `{ cancelled: false }` once reconnect + confirmation succeed. Rejects on failure.
 */
export async function reconnectLinkedItem({ itemId, accountId, onStatus }) {
  const tokenResponse = await fetch(`${apiBaseUrl()}/api/plaid/link/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ origin: plaidOriginUrl(), item_id: itemId }),
  });
  if (!tokenResponse.ok) {
    throw new Error(await parseApiError(tokenResponse));
  }
  const { link_token: linkToken } = await tokenResponse.json();

  const plaid = window.Plaid;
  if (!plaid) {
    throw new Error('Plaid Link script not loaded');
  }

  return new Promise((resolve, reject) => {
    const handler = plaid.create({
      token: linkToken,
      onSuccess: async () => {
        try {
          onStatus?.('Reconnected. Confirming with Plaid...');
          await fetchJson(`/api/accounts/${accountId}/refresh`, { method: 'POST' });
          resolve({ cancelled: false });
        } catch (error) {
          reject(error);
        }
      },
      onExit: (error) => {
        if (error) {
          const code = error.error_code ? ` (${error.error_code})` : '';
          reject(new Error(`${error.error_message || 'Link exited with error.'}${code}`));
        } else {
          resolve({ cancelled: true });
        }
      },
    });
    handler.open();
  });
}
