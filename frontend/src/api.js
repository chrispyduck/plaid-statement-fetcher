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

/**
 * Opens Yodlee FastLink in a full-screen overlay this helper creates and tears down
 * itself, since FastLink (unlike Plaid Link) renders into a container element the
 * host page must provide rather than managing its own overlay.
 */
function openYodleeFastlink({ session, params, onSuccess, onError, onClose }) {
  const fastlink = window.fastlink;
  if (!fastlink) {
    throw new Error('Yodlee FastLink script not loaded');
  }

  const overlay = document.createElement('div');
  overlay.style.position = 'fixed';
  overlay.style.inset = '0';
  overlay.style.zIndex = '2000';
  overlay.style.background = 'rgba(0, 0, 0, 0.5)';
  overlay.style.display = 'flex';
  overlay.style.alignItems = 'center';
  overlay.style.justifyContent = 'center';

  const panel = document.createElement('div');
  panel.style.background = '#fff';
  panel.style.borderRadius = '12px';
  panel.style.width = 'min(480px, 94vw)';
  panel.style.height = 'min(620px, 90vh)';
  panel.style.overflow = 'hidden';
  panel.style.position = 'relative';

  const containerId = `container-fastlink-${Date.now()}`;
  const container = document.createElement('div');
  container.id = containerId;
  container.style.width = '100%';
  container.style.height = '100%';

  panel.appendChild(container);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  const teardown = () => {
    overlay.remove();
  };

  fastlink.open(
    {
      fastLinkURL: session.fastlink_url,
      accessToken: `Bearer ${session.access_token}`,
      params: { configName: session.config_name, ...params },
      onSuccess: (data) => {
        teardown();
        onSuccess?.(data);
      },
      onError: (data) => {
        teardown();
        onError?.(data);
      },
      onClose: (data) => {
        teardown();
        onClose?.(data);
      },
    },
    containerId,
  );
}

/**
 * Links a new institution through Yodlee FastLink, then persists the resulting
 * providerAccountId's accounts via /api/yodlee/link/complete.
 *
 * Resolves with `{ cancelled: true }` if the user closes FastLink without finishing
 * (no onSuccess fired), or `{ cancelled: false }` once linking completes. Rejects on
 * failure.
 */
export async function startYodleeLink({ onStatus } = {}) {
  onStatus?.('Opening Yodlee...');
  const session = await fetchJson('/api/yodlee/fastlink/session', { method: 'POST' });

  return new Promise((resolve, reject) => {
    let succeeded = false;

    openYodleeFastlink({
      session,
      params: { flow: 'add' },
      onSuccess: async (data) => {
        succeeded = true;
        try {
          onStatus?.('Link complete. Saving access...');
          await fetchJson('/api/yodlee/link/complete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider_account_id: String(data.providerAccountId) }),
          });
          resolve({ cancelled: false });
        } catch (error) {
          reject(error);
        }
      },
      onError: (data) => {
        reject(new Error(data?.reason || data?.message || 'FastLink reported an error.'));
      },
      onClose: () => {
        if (!succeeded) {
          resolve({ cancelled: true });
        }
      },
    });
  });
}

/**
 * Re-enters credentials for an existing Yodlee-linked institution via FastLink's
 * "edit" flow, then confirms the reconnect by refreshing one of its accounts.
 *
 * Resolves with `{ cancelled: true }` if the user closes FastLink without finishing,
 * or `{ cancelled: false }` once reconnect + confirmation succeed. Rejects on failure.
 */
export async function reconnectYodleeItem({ providerAccountId, accountId, onStatus }) {
  onStatus?.('Opening Yodlee to reconnect...');
  const session = await fetchJson('/api/yodlee/fastlink/session', { method: 'POST' });

  return new Promise((resolve, reject) => {
    let succeeded = false;

    openYodleeFastlink({
      session,
      params: { flow: 'edit', providerAccountId },
      onSuccess: async () => {
        succeeded = true;
        try {
          onStatus?.('Reconnected. Confirming with Yodlee...');
          await fetchJson(`/api/accounts/${accountId}/refresh`, { method: 'POST' });
          resolve({ cancelled: false });
        } catch (error) {
          reject(error);
        }
      },
      onError: (data) => {
        reject(new Error(data?.reason || data?.message || 'FastLink reported an error.'));
      },
      onClose: () => {
        if (!succeeded) {
          resolve({ cancelled: true });
        }
      },
    });
  });
}
