import { useEffect, useMemo, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  Alert,
  Avatar,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material';
import LinkIcon from '@mui/icons-material/Link';
import VisibilityIcon from '@mui/icons-material/Visibility';
import SyncIcon from '@mui/icons-material/Sync';
import { apiBaseUrl, fetchJson, plaidOriginUrl, parseApiError } from '../api';

function institutionPalette(institutionId, institutionName) {
  const source = String(institutionId || institutionName || '?');
  let hash = 0;
  for (let i = 0; i < source.length; i += 1) {
    hash = source.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = (Math.abs(hash) * 137.508) % 360;
  return `hsl(${hue}, 62%, 46%)`;
}

function institutionInitials(name) {
  const words = String(name || '')
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2);
  if (words.length === 0) {
    return '?';
  }
  return words.map((word) => word[0]?.toUpperCase() || '').join('');
}

function institutionLogoDataUrl(rawLogo) {
  if (!rawLogo) {
    return '';
  }
  if (rawLogo.startsWith('data:image/')) {
    return rawLogo;
  }
  return `data:image/png;base64,${rawLogo}`;
}

function HomePage() {
  const apiBase = useMemo(() => apiBaseUrl(), []);
  const plaidOrigin = useMemo(() => plaidOriginUrl(), []);
  const [accounts, setAccounts] = useState([]);
  const [backendEnv, setBackendEnv] = useState('unknown');
  const [isLoadingAccounts, setIsLoadingAccounts] = useState(false);
  const [isLinking, setIsLinking] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');
  const [errorMessage, setErrorMessage] = useState('');

  const fetchEnvironment = async () => {
    try {
      const payload = await fetchJson('/healthz');
      setBackendEnv(String(payload.env || 'unknown'));
    } catch (error) {
      console.error('Backend environment request failed', error);
      setBackendEnv('unknown');
      setErrorMessage(`Failed to read backend environment: ${String(error)}`);
    }
  };

  const fetchAccounts = async () => {
    setIsLoadingAccounts(true);
    try {
      const payload = await fetchJson('/api/accounts');
      setAccounts(payload);
      setErrorMessage('');
    } catch (error) {
      console.error('Accounts request failed', error);
      setErrorMessage(`Failed to load accounts: ${String(error)}`);
    } finally {
      setIsLoadingAccounts(false);
    }
  };

  useEffect(() => {
    fetchEnvironment();
    fetchAccounts();
  }, []);

  const startLink = async () => {
    setIsLinking(true);
    setStatusMessage('Creating Link token...');
    setErrorMessage('');

    try {
      const tokenResponse = await fetch(`${apiBase}/api/plaid/link/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ origin: plaidOrigin }),
      });
      if (!tokenResponse.ok) {
        throw new Error(await parseApiError(tokenResponse));
      }

      const tokenPayload = await tokenResponse.json();
      const plaid = window.Plaid;
      if (!plaid) {
        throw new Error('Plaid Link script not loaded');
      }

      const handler = plaid.create({
        token: tokenPayload.link_token,
        onSuccess: async (publicToken) => {
          try {
            setStatusMessage('Link complete. Saving access...');
            const exchangeResponse = await fetch(`${apiBase}/api/plaid/link/exchange`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ public_token: publicToken }),
            });
            if (!exchangeResponse.ok) {
              throw new Error(await parseApiError(exchangeResponse));
            }
            await fetchAccounts();
            setStatusMessage('Account linked successfully.');
          } catch (error) {
            console.error('Link exchange failed', error);
            setErrorMessage(`Failed to save linked account: ${String(error)}`);
          } finally {
            setIsLinking(false);
          }
        },
        onExit: (error) => {
          if (error) {
            console.error('Plaid Link exited with error', error);
            const code = error.error_code ? ` (${error.error_code})` : '';
            const msg = error.error_message || 'Link exited with error.';
            setErrorMessage(`${msg}${code}`);
          }
          setIsLinking(false);
        },
      });

      handler.open();
    } catch (error) {
      console.error('Link start failed', error);
      setErrorMessage(`Failed to start Link: ${String(error)}`);
      setStatusMessage('');
      setIsLinking(false);
    }
  };

  return (
    <Stack spacing={2}>
      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h5">Accounts</Typography>
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap">
              <Button
                variant="contained"
                startIcon={isLinking ? <CircularProgress color="inherit" size={18} /> : <LinkIcon />}
                onClick={startLink}
                disabled={isLinking}
              >
                {isLinking ? 'Linking...' : 'Link Institution'}
              </Button>
              <Button
                variant="outlined"
                startIcon={<SyncIcon />}
                component={RouterLink}
                to="/sync"
              >
                Open Sync Progress
              </Button>
              {isLoadingAccounts && <CircularProgress size={20} />}
            </Stack>

            {backendEnv === 'sandbox' && (
              <Alert severity="warning">
                Sandbox mode is active. Linked accounts and statements are test data.
              </Alert>
            )}

            {!!statusMessage && <Alert severity="success">{statusMessage}</Alert>}
            {!!errorMessage && <Alert severity="error">{errorMessage}</Alert>}

            <Box sx={{ overflowX: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Institution</TableCell>
                    <TableCell>Account</TableCell>
                    <TableCell>Alias</TableCell>
                    <TableCell>Details</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {accounts.length === 0 && !isLoadingAccounts ? (
                    <TableRow>
                      <TableCell colSpan={4}>No linked accounts yet.</TableCell>
                    </TableRow>
                  ) : (
                    accounts.map((row) => (
                      <TableRow key={row.account_id}>
                        <TableCell>
                          <Stack direction="row" spacing={1} alignItems="center">
                            <Avatar
                              variant="rounded"
                              sx={{
                                width: 28,
                                height: 28,
                                bgcolor: institutionPalette(
                                  row.institution_id,
                                  row.institution_name,
                                ),
                                color: 'common.white',
                                fontSize: 11,
                                fontWeight: 700,
                              }}
                              src={institutionLogoDataUrl(row.institution_logo)}
                              alt={row.institution_name}
                            >
                              {institutionInitials(row.institution_name)}
                            </Avatar>
                            <Typography variant="body2">{row.institution_name}</Typography>
                          </Stack>
                        </TableCell>
                        <TableCell>{row.account_name}</TableCell>
                        <TableCell>{row.alias || '—'}</TableCell>
                        <TableCell>
                          <Button
                            size="small"
                            variant="outlined"
                            startIcon={<VisibilityIcon />}
                            component={RouterLink}
                            to={`/accounts/${row.account_id}`}
                          >
                            Manage
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </Box>
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}

export default HomePage;
