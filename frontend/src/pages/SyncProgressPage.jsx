import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, CardContent, CircularProgress, Grid, LinearProgress, Snackbar, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material';
import SyncIcon from '@mui/icons-material/Sync';
import RefreshIcon from '@mui/icons-material/Refresh';
import { fetchJson, statementDownloadUrl } from '../api';
import EventLogTable from '../components/EventLogTable';

function formatTimestamp(value) {
  return value ? new Date(value).toLocaleString() : 'Never';
}

function JobSummaryCard({ title, job, icon }) {
  return (
    <Card variant="outlined" sx={{ p: 2, height: '100%' }}>
      <Stack spacing={0.5}>
        <Stack direction="row" spacing={1} alignItems="center">
          {icon}
          <Typography variant="subtitle2">{title}</Typography>
        </Stack>
        {!job ? (
          <Typography variant="body2" color="text.secondary">
            Never run yet.
          </Typography>
        ) : (
          <>
            <Typography variant="body2">
              Last started: {formatTimestamp(job.started_at)}
            </Typography>
            <Typography variant="body2">
              Status: {job.status}
              {job.status === 'completed' && ` (finished ${formatTimestamp(job.finished_at)})`}
            </Typography>
          </>
        )}
      </Stack>
    </Card>
  );
}

function SyncProgressPage() {
  const [jobs, setJobs] = useState([]);
  const [jobsSummary, setJobsSummary] = useState({ refresh: null, sync: null });
  const [downloadedStatements, setDownloadedStatements] = useState([]);
  const [accountLookup, setAccountLookup] = useState({});
  const [isStartingSync, setIsStartingSync] = useState(false);
  const [isStartingRefresh, setIsStartingRefresh] = useState(false);
  const [toastMessage, setToastMessage] = useState('');
  const [isToastOpen, setIsToastOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [selectedJob, setSelectedJob] = useState(null);

  const hasRunningSyncJob = useMemo(
    () => jobs.some((job) => job.job_type !== 'refresh' && job.status === 'running'),
    [jobs],
  );
  const hasRunningRefreshJob = useMemo(
    () => jobs.some((job) => job.job_type === 'refresh' && job.status === 'running'),
    [jobs],
  );
  const hasRunningJob = hasRunningSyncJob || hasRunningRefreshJob;

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

  const loadJobsSummary = async () => {
    try {
      const payload = await fetchJson('/api/jobs/summary');
      setJobsSummary({ refresh: payload?.refresh || null, sync: payload?.sync || null });
    } catch (error) {
      console.error('Failed loading job summary', error);
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

  const loadDownloadedStatements = async () => {
    try {
      const rows = await fetchJson('/api/statements');
      setDownloadedStatements(rows || []);
    } catch (error) {
      console.error('Failed loading downloaded statements', error);
      setErrorMessage(`Failed to load downloaded statements: ${String(error)}`);
    }
  };

  useEffect(() => {
    loadJobs();
    loadJobsSummary();
    loadAccounts();
    loadDownloadedStatements();
  }, []);

  useEffect(() => {
    if (!hasRunningJob && !isStartingSync && !isStartingRefresh) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      loadJobs();
      loadJobsSummary();
    }, 1200);
    return () => window.clearInterval(timer);
  }, [hasRunningJob, isStartingSync, isStartingRefresh, selectedJobId]);

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
      showToast('Fetch started.');
      await loadJobs({ preferredJobId: nextJobId });
      await loadJobsSummary();
      await loadDownloadedStatements();
    } catch (error) {
      console.error('Sync start failed', error);
      setErrorMessage(`Failed to start fetch: ${String(error)}`);
    } finally {
      setIsStartingSync(false);
    }
  };

  const startRefresh = async () => {
    setIsStartingRefresh(true);
    setErrorMessage('');
    setSelectedJob(null);
    try {
      const payload = await fetchJson('/api/refresh/start', { method: 'POST' });
      const nextJobId = payload?.job_id || null;
      if (nextJobId) {
        setSelectedJobId(nextJobId);
      }
      showToast('Refresh started. Newly-posted statements usually take a while to show up with your provider — run a fetch after it completes.');
      await loadJobs({ preferredJobId: nextJobId });
      await loadJobsSummary();
    } catch (error) {
      console.error('Refresh start failed', error);
      setErrorMessage(`Failed to start refresh: ${String(error)}`);
    } finally {
      setIsStartingRefresh(false);
    }
  };

  useEffect(() => {
    if (selectedJob?.status === 'completed' || selectedJob?.status === 'failed') {
      loadDownloadedStatements();
    }
  }, [selectedJob?.job_id, selectedJob?.status]);

  return (
    <Stack spacing={2}>
      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h5">Statement Download Progress</Typography>
            <Typography variant="body2" color="text.secondary">
              Refresh asks each institution's provider (Plaid or Yodlee) to check for
              newly-posted statements; fetch lists and downloads whatever the provider
              currently has. They run on independent schedules — by default, refresh
              weekly and fetch ~24h after each refresh completes — since providers need
              time to process a refresh before new statements show up.
            </Typography>
            <Grid container spacing={2}>
              <Grid item xs={12} sm={6}>
                <JobSummaryCard
                  title="Last Refresh"
                  job={jobsSummary.refresh}
                  icon={<RefreshIcon fontSize="small" />}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <JobSummaryCard
                  title="Last Fetch"
                  job={jobsSummary.sync}
                  icon={<SyncIcon fontSize="small" />}
                />
              </Grid>
            </Grid>
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap">
              <Button
                variant="outlined"
                startIcon={
                  isStartingRefresh ? <CircularProgress size={18} color="inherit" /> : <RefreshIcon />
                }
                disabled={isStartingRefresh || hasRunningRefreshJob}
                onClick={startRefresh}
              >
                {hasRunningRefreshJob ? 'Refreshing...' : 'Run Refresh Now'}
              </Button>
              <Button
                variant="contained"
                startIcon={isStartingSync ? <CircularProgress size={18} color="inherit" /> : <SyncIcon />}
                disabled={isStartingSync || hasRunningSyncJob}
                onClick={startSync}
              >
                {hasRunningSyncJob ? 'Fetching...' : 'Run Fetch Now'}
              </Button>
            </Stack>

            {!!errorMessage && <Alert severity="error">{errorMessage}</Alert>}

            {!selectedJob ? (
              <Typography color="text.secondary">No jobs yet.</Typography>
            ) : (
              <Card variant="outlined" sx={{ p: 2 }}>
                <Stack spacing={1}>
                  <Typography variant="subtitle2">
                    Selected {selectedJob.job_type === 'refresh' ? 'Refresh' : 'Fetch'} Job:{' '}
                    {selectedJob.job_id}
                  </Typography>
                  {selectedJob.status === 'running' && <LinearProgress />}
                  <Typography variant="body2">Status: {selectedJob.status}</Typography>
                  {selectedJob.job_type === 'refresh' ? (
                    <Typography variant="body2">
                      Requested: {selectedJob.requested ?? 0} | Failed: {selectedJob.failed ?? 0}
                    </Typography>
                  ) : (
                    <Typography variant="body2">
                      Listed: {selectedJob.listed} | Downloaded: {selectedJob.downloaded} | Existing:{' '}
                      {selectedJob.skipped_existing} | Filtered: {selectedJob.skipped_filtered} | Errors:{' '}
                      {selectedJob.errors}
                    </Typography>
                  )}
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
            <Typography variant="h6">Job History</Typography>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Started</TableCell>
                  <TableCell>Type</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Result</TableCell>
                  <TableCell>Open</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {jobs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5}>No jobs yet.</TableCell>
                  </TableRow>
                ) : (
                  jobs.map((job) => (
                    <TableRow key={job.job_id} selected={job.job_id === selectedJobId}>
                      <TableCell>{new Date(job.started_at).toLocaleString()}</TableCell>
                      <TableCell>{job.job_type === 'refresh' ? 'Refresh' : 'Fetch'}</TableCell>
                      <TableCell>{job.status}</TableCell>
                      <TableCell>
                        {job.job_type === 'refresh'
                          ? `${job.requested ?? 0} requested`
                          : `${job.downloaded} downloaded`}
                      </TableCell>
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

      <Card variant="outlined" sx={{ borderRadius: 3 }}>
        <CardContent>
          <Stack spacing={2}>
            <Typography variant="h6">Previously Fetched Statements</Typography>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Date</TableCell>
                  <TableCell>Account</TableCell>
                  <TableCell>Institution</TableCell>
                  <TableCell>File</TableCell>
                  <TableCell>Downloaded</TableCell>
                  <TableCell>Action</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {downloadedStatements.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6}>No downloaded statements yet.</TableCell>
                  </TableRow>
                ) : (
                  downloadedStatements.map((statement) => (
                    <TableRow key={statement.dedupe_key}>
                      <TableCell>{statement.statement_date}</TableCell>
                      <TableCell>{statement.account_name || 'Account'}</TableCell>
                      <TableCell>{statement.institution_name || 'Institution'}</TableCell>
                      <TableCell>{statement.file_name || 'statement.pdf'}</TableCell>
                      <TableCell>
                        {statement.downloaded_at
                          ? new Date(statement.downloaded_at).toLocaleString()
                          : '—'}
                      </TableCell>
                      <TableCell>
                        <Button
                          size="small"
                          variant="outlined"
                          href={statementDownloadUrl(statement.dedupe_key)}
                          disabled={!statement.file_exists}
                        >
                          Download
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
