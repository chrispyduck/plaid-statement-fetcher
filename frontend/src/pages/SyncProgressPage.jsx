import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, CardContent, CircularProgress, LinearProgress, Snackbar, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material';
import SyncIcon from '@mui/icons-material/Sync';
import { fetchJson } from '../api';
import EventLogTable from '../components/EventLogTable';

function SyncProgressPage() {
  const [jobs, setJobs] = useState([]);
  const [accountLookup, setAccountLookup] = useState({});
  const [isStartingSync, setIsStartingSync] = useState(false);
  const [toastMessage, setToastMessage] = useState('');
  const [isToastOpen, setIsToastOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [selectedJob, setSelectedJob] = useState(null);

  const activeJob = useMemo(() => jobs[0] || null, [jobs]);
  const hasRunningJob = useMemo(
    () => jobs.some((job) => job.status === 'running'),
    [jobs],
  );

  const showToast = (message) => {
    setToastMessage(message);
    setIsToastOpen(true);
  };

  const closeToast = (_event, reason) => {
    if (reason === 'clickaway') {
      return;
    }
    setIsToastOpen(false);
  };

  const loadJobs = async ({ preferredJobId = null } = {}) => {
    try {
      const payload = await fetchJson('/api/sync/jobs');
      const nextJobs = payload || [];
      setJobs(nextJobs);

      const fallbackJobId = preferredJobId || selectedJobId || nextJobs[0]?.job_id || null;
      if (fallbackJobId) {
        const job = await fetchJson(`/api/sync/status/${fallbackJobId}`);
        setSelectedJobId(fallbackJobId);
        setSelectedJob(job);
      } else {
        setSelectedJobId(null);
        setSelectedJob(null);
      }
    } catch (error) {
      console.error('Failed loading sync jobs', error);
      setErrorMessage(`Failed to load sync jobs: ${String(error)}`);
    }
  };

  const loadAccounts = async () => {
    try {
      const rows = await fetchJson('/api/accounts');
      const nextLookup = {};
      for (const row of rows || []) {
        if (!row.account_id) {
          continue;
        }
        nextLookup[row.account_id] = {
          name: row.alias || row.account_name || 'Account',
        };
      }
      setAccountLookup(nextLookup);
    } catch (error) {
      console.error('Failed loading accounts for log rendering', error);
    }
  };

  useEffect(() => {
    loadJobs();
    loadAccounts();
  }, []);

  useEffect(() => {
    if (!hasRunningJob && !isStartingSync) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      loadJobs();
    }, 1200);
    return () => window.clearInterval(timer);
  }, [hasRunningJob, isStartingSync, selectedJobId]);

  const openJob = async (jobId) => {
    try {
      const job = await fetchJson(`/api/sync/status/${jobId}`);
      setSelectedJobId(jobId);
      setSelectedJob(job);
    } catch (error) {
      console.error('Failed loading selected sync job', error);
      setErrorMessage(`Failed to load selected sync job: ${String(error)}`);
    }
  };

  const startSync = async () => {
    setIsStartingSync(true);
    setErrorMessage('');
    setSelectedJob(null);
    try {
      const payload = await fetchJson('/api/sync/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: false }),
      });
      const nextJobId = payload?.job_id || null;
      if (nextJobId) {
        setSelectedJobId(nextJobId);
      }
      showToast('Sync started.');
      await loadJobs({ preferredJobId: nextJobId });
    } catch (error) {
      console.error('Sync start failed', error);
      setErrorMessage(`Failed to start sync: ${String(error)}`);
    } finally {
      setIsStartingSync(false);
    }
  };

  return (
    <Stack spacing={2}>
      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h5">Statement Download Progress</Typography>
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap">
              <Button
                variant="contained"
                startIcon={isStartingSync ? <CircularProgress size={18} color="inherit" /> : <SyncIcon />}
                disabled={isStartingSync || hasRunningJob}
                onClick={startSync}
              >
                {hasRunningJob ? 'Syncing...' : 'Start Sync'}
              </Button>
            </Stack>

            {!!errorMessage && <Alert severity="error">{errorMessage}</Alert>}

            {!selectedJob ? (
              <Typography color="text.secondary">No sync jobs yet.</Typography>
            ) : (
              <Card variant="outlined" sx={{ p: 2 }}>
                <Stack spacing={1}>
                  <Typography variant="subtitle2">Selected Job: {selectedJob.job_id}</Typography>
                  {selectedJob.status === 'running' && <LinearProgress />}
                  <Typography variant="body2">Status: {selectedJob.status}</Typography>
                  <Typography variant="body2">
                    Listed: {selectedJob.listed} | Downloaded: {selectedJob.downloaded} | Existing:{' '}
                    {selectedJob.skipped_existing} | Filtered: {selectedJob.skipped_filtered} | Errors:{' '}
                    {selectedJob.errors}
                  </Typography>
                  {!!selectedJob.error && <Alert severity="error">{selectedJob.error}</Alert>}
                </Stack>
              </Card>
            )}
          </Stack>
        </CardContent>
      </Card>

      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h6">Sync History</Typography>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Started</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Downloads</TableCell>
                  <TableCell>Open</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {jobs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4}>No sync runs yet.</TableCell>
                  </TableRow>
                ) : (
                  jobs.map((job) => (
                    <TableRow key={job.job_id} selected={job.job_id === selectedJobId}>
                      <TableCell>{new Date(job.started_at).toLocaleString()}</TableCell>
                      <TableCell>{job.status}</TableCell>
                      <TableCell>{job.downloaded}</TableCell>
                      <TableCell>
                        <Button size="small" variant="outlined" onClick={() => openJob(job.job_id)}>
                          Open Logs
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </Stack>
        </CardContent>
      </Card>

      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h6">Detailed Logs</Typography>
            <EventLogTable
              events={selectedJob?.logs || []}
              emptyText="No logs yet."
              accountLookup={accountLookup}
            />
          </Stack>
        </CardContent>
      </Card>

      <Snackbar
        open={isToastOpen}
        autoHideDuration={3000}
        onClose={closeToast}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        <Alert onClose={closeToast} severity="success" variant="filled" sx={{ width: '100%' }}>
          {toastMessage}
        </Alert>
      </Snackbar>
    </Stack>
  );
}

export default SyncProgressPage;
