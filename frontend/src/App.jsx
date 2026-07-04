import { NavLink, Route, Routes } from 'react-router-dom';
import {
  AppBar,
  Card,
  CardContent,
  Container,
  Stack,
  Toolbar,
  Typography,
} from '@mui/material';
import HomePage from './pages/HomePage';
import AccountDetailsPage from './pages/AccountDetailsPage';
import SyncProgressPage from './pages/SyncProgressPage';
import ServiceConfigPage from './pages/ServiceConfigPage';

function App() {
  return (
    <Stack sx={{ backgroundColor: '#edf1f7', minHeight: '100vh' }}>
      <AppBar position="sticky" elevation={0} color="transparent" sx={{ backdropFilter: 'blur(8px)' }}>
        <Toolbar sx={{ gap: 1 }}>
          <Typography variant="h6" sx={{ flexGrow: 1 }}>
            Statement Fetcher
          </Typography>
          <ButtonLink to="/" label="Accounts" />
          <ButtonLink to="/sync" label="Sync" />
          <ButtonLink to="/service-config" label="Service Config" />
        </Toolbar>
      </AppBar>
      <Container maxWidth="lg">
        <Card sx={{ borderRadius: 3, boxShadow: 6, mt: 3, mb: 4 }}>
          <CardContent>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/accounts/:accountId" element={<AccountDetailsPage />} />
              <Route path="/sync" element={<SyncProgressPage />} />
              <Route path="/service-config" element={<ServiceConfigPage />} />
            </Routes>
          </CardContent>
        </Card>
      </Container>
    </Stack>
  );
}

function ButtonLink({ to, label }) {
  return (
    <Typography
      component={NavLink}
      to={to}
      sx={{
        textDecoration: 'none',
        color: 'text.primary',
        px: 1,
        py: 0.5,
        borderRadius: 1,
        '&.active': {
          backgroundColor: 'rgba(25, 118, 210, 0.12)',
          color: 'primary.main',
        },
      }}
    >
      {label}
    </Typography>
  );
}

export default App;
