import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Grid2,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import { fetchJson } from '../api';

const FIELD_SPECS = [
  { key: 'plaid_language', label: 'Plaid Language' },
  { key: 'plaid_country_codes', label: 'Country Codes' },
  { key: 'plaid_products', label: 'Products' },
  { key: 'plaid_redirect_uri', label: 'Redirect URI (blank resets to default)' },
  { key: 'retry_max_attempts', label: 'Retry Max Attempts', type: 'number' },
  { key: 'retry_base_delay_seconds', label: 'Retry Base Delay Seconds', type: 'number' },
  { key: 'retry_max_delay_seconds', label: 'Retry Max Delay Seconds', type: 'number' },
  { key: 'statements_start_date', label: 'Statements Start Date (YYYY-MM-DD)' },
  { key: 'statements_end_date', label: 'Statements End Date (YYYY-MM-DD)' },
];

function ServiceConfigPage() {
  const [formValues, setFormValues] = useState({});
  const [persistedValues, setPersistedValues] = useState({});
  const [environment, setEnvironment] = useState('unknown');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');
  const [errorMessage, setErrorMessage] = useState('');

  const persistedPretty = useMemo(() => JSON.stringify(persistedValues, null, 2), [persistedValues]);

  const loadConfig = async () => {
    setIsLoading(true);
    try {
      const payload = await fetchJson('/api/service/config');
      setFormValues(payload.runtime || {});
      setPersistedValues(payload.persisted || {});
      setEnvironment(payload.environment || 'unknown');
      setErrorMessage('');
    } catch (error) {
      console.error('Failed loading service config', error);
      setErrorMessage(`Failed to load service configuration: ${String(error)}`);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadConfig();
  }, []);

  const saveConfig = async () => {
    setIsSaving(true);
    setStatusMessage('');
    setErrorMessage('');
    try {
      const payload = await fetchJson('/api/service/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formValues),
      });
      setFormValues(payload.runtime || {});
      setPersistedValues(payload.persisted || {});
      setStatusMessage('Service configuration saved.');
    } catch (error) {
      console.error('Service config save failed', error);
      setErrorMessage(`Failed to save service configuration: ${String(error)}`);
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <Stack direction="row" spacing={1} alignItems="center">
        <CircularProgress size={20} />
        <Typography>Loading service configuration...</Typography>
      </Stack>
    );
  }

  return (
    <Stack spacing={2}>
      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h5">Service Configuration</Typography>
            <Typography color="text.secondary">Runtime mode: {String(environment).toUpperCase()}</Typography>
            {!!statusMessage && <Alert severity="success">{statusMessage}</Alert>}
            {!!errorMessage && <Alert severity="error">{errorMessage}</Alert>}

            <Grid2 container spacing={2}>
              {FIELD_SPECS.map((field) => (
                <Grid2 key={field.key} size={{ xs: 12, md: 6 }}>
                  <TextField
                    fullWidth
                    label={field.label}
                    type={field.type || 'text'}
                    value={formValues[field.key] ?? ''}
                    onChange={(event) =>
                      setFormValues((prev) => ({
                        ...prev,
                        [field.key]: event.target.value,
                      }))
                    }
                  />
                </Grid2>
              ))}
            </Grid2>

            <Stack direction="row">
              <Button
                variant="contained"
                startIcon={isSaving ? <CircularProgress size={16} color="inherit" /> : <SaveIcon />}
                disabled={isSaving}
                onClick={saveConfig}
              >
                {isSaving ? 'Saving...' : 'Save Configuration'}
              </Button>
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={1}>
            <Typography variant="h6">Persisted Overrides</Typography>
            <Typography variant="caption" component="pre" sx={{ m: 0 }}>
              {persistedPretty}
            </Typography>
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}

export default ServiceConfigPage;
