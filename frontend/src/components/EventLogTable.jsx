import { useMemo, useState } from 'react';
import {
  Box,
  FormControlLabel,
  Link,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import { Link as RouterLink } from 'react-router-dom';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';

function sanitizeMessage(message, accountLookup) {
  let text = String(message || '');
  if (!text) {
    return text;
  }

  for (const [accountId, account] of Object.entries(accountLookup || {})) {
    if (!accountId) {
      continue;
    }
    const replacement = account?.name || 'Account';
    text = text.split(accountId).join(replacement);
  }
  return text;
}

function eventMessageParts(entry, accountLookup) {
  const accountId = entry?.account_id || entry?.metadata?.account_id || null;
  const accountName = accountLookup?.[accountId]?.name || 'Account';
  const statementDate =
    typeof entry?.metadata?.statement_date === 'string' ? entry.metadata.statement_date : '';
  const baseMessage = sanitizeMessage(entry?.message, accountLookup);

  return {
    accountId,
    accountName,
    statementDate,
    baseMessage,
  };
}

function renderInlineMessage(entry, accountLookup) {
  const { accountId, accountName, statementDate, baseMessage } = eventMessageParts(
    entry,
    accountLookup,
  );

  if (entry?.event_type === 'statement_existing' && statementDate) {
    return (
      <>
        {`Statement dated ${statementDate} already downloaded`}
        {accountId && (
          <>
            {' for '}
            <Link component={RouterLink} to={`/accounts/${accountId}`} underline="hover">
              {accountName}
            </Link>
          </>
        )}
      </>
    );
  }

  return (
    <>
      {baseMessage}
      {accountId && (
        <>
          {' for '}
          <Link component={RouterLink} to={`/accounts/${accountId}`} underline="hover">
            {accountName}
          </Link>
        </>
      )}
      {statementDate ? ` on ${statementDate}` : ''}
    </>
  );
}

function humanizeEventType(value) {
  const text = String(value || '').trim();
  if (!text) {
    return 'Unknown';
  }
  return text
    .split('_')
    .map((chunk) => (chunk ? `${chunk[0].toUpperCase()}${chunk.slice(1)}` : chunk))
    .join(' ');
}

function severityAdornment(level) {
  if (level === 'error') {
    return <ErrorOutlineIcon sx={{ color: 'error.main', fontSize: 16 }} />;
  }
  if (level === 'warning' || level === 'warn') {
    return <WarningAmberIcon sx={{ color: 'warning.main', fontSize: 16 }} />;
  }
  return null;
}

function EventLogTable({ events, emptyText = 'No logs yet.', accountLookup = {} }) {
  const [showDebug, setShowDebug] = useState(false);

  const visibleEvents = useMemo(() => {
    if (!events || showDebug) {
      return events || [];
    }
    return events.filter((entry) => entry.level !== 'debug');
  }, [events, showDebug]);

  const resolvedEmptyText =
    events && events.length > 0 && visibleEvents.length === 0
      ? 'No logs for current filter.'
      : emptyText;

  return (
    <Box sx={{ overflowX: 'auto' }}>
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 1 }}>
        <FormControlLabel
          control={<Switch checked={showDebug} onChange={(event) => setShowDebug(event.target.checked)} />}
          label="Show debug events"
        />
      </Box>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Time</TableCell>
            <TableCell>Type</TableCell>
            <TableCell>Message</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {!visibleEvents || visibleEvents.length === 0 ? (
            <TableRow>
              <TableCell colSpan={3}>{resolvedEmptyText}</TableCell>
            </TableRow>
          ) : (
            visibleEvents.map((entry) => (
              <TableRow key={entry.event_id}>
                <TableCell>{new Date(entry.created_at).toLocaleString()}</TableCell>
                <TableCell>{humanizeEventType(entry.event_type)}</TableCell>
                <TableCell>
                  <Box sx={{ display: 'flex', alignItems: 'center', width: '100%', gap: 0.75 }}>
                    {severityAdornment(entry.level)}
                    <Box sx={{ flexGrow: 1 }}>
                      <Typography variant="body2">{renderInlineMessage(entry, accountLookup)}</Typography>
                    </Box>
                    {entry.metadata && (
                      <Tooltip
                        arrow
                        placement="top-start"
                        title={
                          <Box
                            component="pre"
                            sx={{
                              m: 0,
                              whiteSpace: 'pre-wrap',
                              maxWidth: 520,
                              fontSize: 12,
                            }}
                          >
                            {JSON.stringify(entry.metadata, null, 2)}
                          </Box>
                        }
                      >
                        <InfoOutlinedIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
                      </Tooltip>
                    )}
                  </Box>
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </Box>
  );
}

export default EventLogTable;
