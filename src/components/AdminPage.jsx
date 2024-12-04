import { useState, useEffect } from 'react';

function AdminPage() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [password, setPassword] = useState('');
  const [buyClicks, setBuyClicks] = useState(0);
  const [emailSubmissions, setEmailSubmissions] = useState(0);

  useEffect(() => {
    if (isAuthenticated) {
      loadTrackingData();
      const interval = setInterval(loadTrackingData, 1000);
      return () => clearInterval(interval);
    }
  }, [isAuthenticated]);

  const loadTrackingData = () => {
    const clicks = parseInt(localStorage.getItem('buyClicksCount') || '0');
    const submissions = parseInt(localStorage.getItem('emailSubmissionsCount') || '0');
    setBuyClicks(clicks);
    setEmailSubmissions(submissions);
  };

  const handleLogin = (e) => {
    e.preventDefault();
    if (password === 'eeg') {
      setIsAuthenticated(true);
    } else {
      alert('Incorrect password');
    }
  };

  if (!isAuthenticated) {
    return (
      <div className="admin-login">
        <h2>Admin Login</h2>
        <form onSubmit={handleLogin}>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter password"
          />
          <button type="submit">Login</button>
        </form>
      </div>
    );
  }

  return (
    <div className="admin-page">
      <h1>Admin Dashboard</h1>
      <div className="stats-container">
        <div className="stat-box">
          <h3>Buy Now Clicks</h3>
          <div className="stat-number">{buyClicks}</div>
        </div>
        <div className="stat-box">
          <h3>Email Submissions</h3>
          <div className="stat-number">{emailSubmissions}</div>
        </div>
      </div>
    </div>
  );
}

export default AdminPage; 