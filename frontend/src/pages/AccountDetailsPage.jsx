import { useEffect, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import DeleteIcon from '@mui/icons-material/Delete';
import SaveIcon from '@mui/icons-material/Save';
import SyncIcon from '@mui/icons-material/Sync';
import { fetchJson } from '../api';
import EventLogTable from '../components/EventLogTable';

function AccountDetailsPage() {
  const { accountId } = useParams();
  const navigate = useNavigate();
  const [details, setDetails] = useState(null);
  const [alias, setAlias] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isRemoving, setIsRemoving] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');
  const [errorMessage, setErrorMessage] = useState('');

  const loadDetails = async () => {
    setIsLoading(true);
    try {
      const payload = await fetchJson(`/api/accounts/${accountId}`);
      setDetails(payload);
      setAlias(payload.alias || '');
      setErrorMessage('');
    } catch (error) {
      console.error('Failed loading account details', error);
      setErrorMessage(`Failed to load account details: ${String(error)}`);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadDetails();
  }, [accountId]);

  const saveAlias = async () => {
    setIsSaving(true);
    setStatusMessage('');
    setErrorMessage('');
    try {
      await fetchJson('/api/accounts/alias', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_id: accountId, alias }),
      });
      setStatusMessage('Alias updated.');
      await loadDetails();
    } catch (error) {
      console.error('Alias update failed', error);
      setErrorMessage(`Failed to save alias: ${String(error)}`);
    } finally {
      setIsSaving(false);
    }
  };

  const removeAccount = async () => {
    setIsRemoving(true);
    setStatusMessage('');
    setErrorMessage('');
    try {
      await fetchJson(`/api/accounts/${accountId}`, { method: 'DELETE' });
      setStatusMessage('Account removed.');
      navigate('/');
    } catch (error) {
      console.error('Account remove failed', error);
      setErrorMessage(`Failed to remove account: ${String(error)}`);
    } finally {
      setIsRemoving(false);
      setShowConfirm(false);
    }
  };

  const refreshAccount = async () => {
    setIsRefreshing(true);
    setStatusMessage('');
    setErrorMessage('');
    try {
      await fetchJson(`/api/accounts/${accountId}/refresh`, { method: 'POST' });
      setStatusMessage('Account refreshed.');
      await loadDetails();
    } catch (error) {
      console.error('Account refresh failed', error);
      setErrorMessage(`Failed to refresh account: ${String(error)}`);
    } finally {
      setIsRefreshing(false);
    }
  };

  if (isLoading) {
    return (
      <Stack direction="row" spacing={1} alignItems="center">
        <CircularProgress size={20} />
        <Typography>Loading account details...</Typography>
      </Stack>
    );
  }

  if (!details) {
    return (
      <Stack spacing={2}>
        {!!errorMessage && <Alert severity="error">{errorMessage}</Alert>}
        <Button startIcon={<ArrowBackIcon />} component={RouterLink} to="/" variant="outlined">
          Back to Home
        </Button>
      </Stack>
    );
  }

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1}>
        <Button startIcon={<ArrowBackIcon />} component={RouterLink} to="/" variant="outlined">
          Back
        </Button>
      </Stack>

      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Stack
              direction={{ xs: 'column', sm: 'row' }}
              alignItems={{ xs: 'stretch', sm: 'center' }}
              justifyContent="space-between"
              spacing={1}
            >
              <Typography variant="h5">Account Configuration</Typography>
              <Stack direction="row" spacing={1} justifyContent="flex-end">
                <Button
                  variant="outlined"
                  startIcon={
                    isRefreshing ? <CircularProgress size={16} color="inherit" /> : <SyncIcon />
                  }
                  disabled={isRefreshing || isRemoving}
                  onClick={refreshAccount}
                >
                  {isRefreshing ? 'Refreshing...' : 'Refresh'}
                </Button>
                <Button
                  color="error"
                  variant="contained"
                  startIcon={
                    isRemoving ? <CircularProgress size={16} color="inherit" /> : <DeleteIcon />
                  }
                  disabled={isRemoving || isRefreshing}
                  onClick={() => setShowConfirm(true)}
                >
                  Remove
                </Button>
              </Stack>
            </Stack>
            {!!statusMessage && <Alert severity="success">{statusMessage}</Alert>}
            {!!errorMessage && <Alert severity="error">{errorMessage}</Alert>}

            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
              <TextField
                label="Alias"
                value={alias}
                onChange={(event) => setAlias(event.target.value)}
                fullWidth
              />
              <Button
                variant="contained"
                startIcon={isSaving ? <CircularProgress size={16} color="inherit" /> : <SaveIcon />}
                disabled={isSaving}
                onClick={saveAlias}
              >
                {isSaving ? 'Saving...' : 'Save'}
              </Button>
            </Stack>

            <Box>
              <Typography variant="subtitle2" color="text.secondary">Institution</Typography>
              <Typography>{details.institution_name}</Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">Account</Typography>
              <Typography>{details.account_name}</Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">Account Mask</Typography>
              <Typography>{details.account_mask || '—'}</Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">Account Type</Typography>
              <Typography>{details.account_type || '—'} / {details.account_subtype || '—'}</Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">Date Added</Typography>
              <Typography>
                {details.linked_created_at
                  ? new Date(details.linked_created_at).toLocaleString()
                  : '—'}
              </Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">Last Synced with Plaid</Typography>
              <Typography>
                {details.linked_updated_at
                  ? new Date(details.linked_updated_at).toLocaleString()
                  : '—'}
              </Typography>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h6">Past Events</Typography>
            <EventLogTable
              events={details.events || []}
              emptyText="No events yet."
              accountLookup={{
                [accountId]: {
                  name: alias || details.account_name || 'Account',
                },
              }}
            />
          </Stack>
        </CardContent>
      </Card>

      <Dialog open={showConfirm} onClose={() => setShowConfirm(false)}>
        <DialogTitle>Remove account?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            This removes the linked account configuration and cannot be undone.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setShowConfirm(false)}>Cancel</Button>
          <Button color="error" onClick={removeAccount}>
            Remove
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

export default AccountDetailsPage;
