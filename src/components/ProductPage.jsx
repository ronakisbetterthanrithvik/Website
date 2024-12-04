function ProductPage() {
  const [isSoldOut, setIsSoldOut] = React.useState(false);
  const [email, setEmail] = React.useState('');
  const [isSubscribed, setIsSubscribed] = React.useState(false);

  React.useEffect(() => {
    // Initialize counters if they don't exist
    if (!localStorage.getItem('buyClicksCount')) {
      localStorage.setItem('buyClicksCount', '0');
    }
    if (!localStorage.getItem('emailSubmissionsCount')) {
      localStorage.setItem('emailSubmissionsCount', '0');
    }
  }, []);

  const handleBuyClick = async () => {
    try {
      await fetch('/api/track/buy', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        }
      });
    } catch (error) {
      console.error('Error tracking buy click:', error);
    }
  };

  const handleEmailSubmit = async (e) => {
    e.preventDefault();
    try {
      await fetch('/api/track/email', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email })
      });
    } catch (error) {
      console.error('Error tracking email submission:', error);
    }
  };

  return (
    <div className="product-page">
      <h1>Augmentary AR Game</h1>
      <div className="product-content">
        <button 
          onClick={handleBuyClick} 
          disabled={isSoldOut}
          className="buy-button"
        >
          {isSoldOut ? 'Sold Out' : 'Buy Now'}
        </button>
        <form onSubmit={handleEmailSubmit}>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Enter your email"
          />
          <button type="submit">Submit</button>
        </form>
      </div>
    </div>
  );
}