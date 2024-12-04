import { useState, useEffect } from 'react';

function AdminDashboard() {
  const [trackingData, setTrackingData] = useState({
    purchases: [],
    emails: []
  });

  useEffect(() => {
    // Initial fetch
    fetchTrackingData();

    // Set up polling every 5 seconds
    const interval = setInterval(() => {
      fetchTrackingData();
    }, 5000);

    // Cleanup interval on component unmount
    return () => clearInterval(interval);
  }, []);

  const fetchTrackingData = async () => {
    try {
      const response = await fetch('/api/tracking/data');
      if (!response.ok) {
        throw new Error('Failed to fetch tracking data');
      }
      const data = await response.json();
      setTrackingData(data);
    } catch (error) {
      console.error('Error fetching tracking data:', error);
    }
  };

  return (
    <div className="admin-dashboard">
      <h1>Admin Dashboard</h1>
      
      <div className="tracking-section">
        <h2>Purchase Clicks</h2>
        <table className="tracking-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Product ID</th>
            </tr>
          </thead>
          <tbody>
            {trackingData.purchases.map((click, index) => (
              <tr key={index}>
                <td>{new Date(click.timestamp).toLocaleString()}</td>
                <td>{click.productId}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="tracking-section">
        <h2>Email Submissions</h2>
        <table className="tracking-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Email</th>
            </tr>
          </thead>
          <tbody>
            {trackingData.emails.map((submission, index) => (
              <tr key={index}>
                <td>{new Date(submission.timestamp).toLocaleString()}</td>
                <td>{submission.email}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default AdminDashboard; 