import { BrowserRouter as Router, Routes, Route, Link, useLocation } from 'react-router-dom';
import { useState } from 'react';
import reactLogo from "./assets/react.svg";
import viteLogo from "/vite.svg";
import "./App.css";
import AdminPage from './components/AdminPage';
import axios from 'axios';
import augmentImage from './assets/augment.png'
import image1 from './assets/1.png'
import image2 from './assets/2.png'
import image3 from './assets/3.png'
import image1400 from './assets/1400.png'
import image2400 from './assets/2400.png'
import image3400 from './assets/3400.png'
import holoLogo from './assets/holoredone.png'

// Create a Home component
function Home() {
  return (
    <div>
      <h1>Welcome to the Home Page</h1>
    </div>
  );
}

// Create a ProductPage component
function ProductPage() {
  const [isSoldOut, setIsSoldOut] = useState(false);
  const [email, setEmail] = useState('');
  const [isSubscribed, setIsSubscribed] = useState(false);
  const [quantity, setQuantity] = useState(1);
  const [currentImage, setCurrentImage] = useState(augmentImage);

  const thumbnailToFullSize = {
    [image1]: image1400,
    [image2]: image2400,
    [image3]: image3400,
    [augmentImage]: augmentImage
  };

  const handleBuyClick = async () => {
    try {
      await axios.post('https://logtodatabase-rgzyvy3rca-uc.a.run.app', {
        name: 'ronak',
        content: "clicked buy"
      });
      setIsSoldOut(true);
      setIsSubscribed(false);
    } catch (error) {
      console.error('Error submitting email:', error);
      // Optionally handle the error case here
    }
  };

  const handleEmailSubmit = async (e) => {
    e.preventDefault();
    try {
      await axios.post('https://logtodatabase-rgzyvy3rca-uc.a.run.app', {
        name: 'ronak',
        content: email
      });
      setIsSubscribed(true);
    } catch (error) {
      console.error('Error submitting email:', error);
      // Optionally handle the error case here
    }
  };

  const increaseQuantity = () => {
    setQuantity(quantity + 1);
  };

  const decreaseQuantity = () => {
    if (quantity > 1) {
      setQuantity(quantity - 1);
    }
  };

  const handleThumbnailClick = (thumbnailPath) => {
    setCurrentImage(thumbnailToFullSize[thumbnailPath] || thumbnailPath);
  };

  if (isSoldOut) {
    return (
      <div className="sold-out-container">
        <h1 className="sold-out-message">Sorry, this product is sold out :(</h1>
        {!isSubscribed ? (
          <div className="notification-signup">
            <h2>Get email notifications to know when this product restocks!</h2>
            <form onSubmit={handleEmailSubmit}>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Enter your email"
                required
              />
              <button type="submit">Notify Me</button>
            </form>
          </div>
        ) : (
          <p className="success-message">Thanks! We'll notify you when the product is back in stock.</p>
        )}
      </div>
    );
  }

  return (
    <div className="product-page">
      <div className="product-images">
        <img src={currentImage} alt="Product" className="main-image" />
        <div className="thumbnail-container">
          <img 
            src={augmentImage} 
            alt="Main Product" 
            onClick={() => handleThumbnailClick(augmentImage)}
            className="thumbnail"
          />
          <img 
            src={image1} 
            alt="Thumbnail 1" 
            onClick={() => handleThumbnailClick(image1)}
            className="thumbnail"
          />
          <img 
            src={image2} 
            alt="Thumbnail 2" 
            onClick={() => handleThumbnailClick(image2)}
            className="thumbnail"
          />
          <img 
            src={image3} 
            alt="Thumbnail 3" 
            onClick={() => handleThumbnailClick(image3)}
            className="thumbnail"
          />
        </div>
      </div>

      <div className="product-details">
        <p className="product-category">HOLOQUEST GAMES</p>
        <h1 className="product-title">Augmentary: The AR Board Game</h1>
        <p className="product-price">$49.99</p>
        <div className="quantity-selector">
          <label>Quantity</label>
          <div className="quantity-controls">
            <button onClick={decreaseQuantity}>-</button>
            <input type="number" value={quantity} readOnly />
            <button onClick={increaseQuantity}>+</button>
          </div>
        </div>
        <button className="buy-button" onClick={handleBuyClick}>
          Buy Now
        </button>
        <div className="product-description">
          <p>Step into a new dimension of tabletop gaming with Augmentary, the ultimate augmented reality (AR) board game experience. This cutting-edge game merges the physical and digital worlds, transforming your table into a dynamic battleground of holographic effects, interactive gameplay, and immersive storytelling.</p>
        </div>
      </div>
    </div>
  );
}

// Main App component
function App() {
  return (
    <Router>
      <div className="app-container">
        <nav className="navbar">
          <div className="logo-container">
            <img src={holoLogo} alt="HoloQuest Logo" className="navbar-logo" />
          </div>
          <ul>
            <li><Link to="/">Home</Link></li>
            <li><Link to="/shop">Shop</Link></li>
            <li><Link to="/about">About Us</Link></li>
            <li><Link to="/contact">Contact</Link></li>
            <li><Link to="/faq">FAQ</Link></li>
          </ul>
        </nav>

        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/shop" element={<ProductPage />} />
          <Route path="/about" element={<div>About Page</div>} />
          <Route path="/contact" element={<div>Contact Page</div>} />
          <Route path="/faq" element={<div>FAQ Page</div>} />
          <Route path="/admin" element={<AdminPage />} />
        </Routes>

        <footer className="footer">
          <div className="footer-content">
            <p>&copy; 2024 HoloQuest. All rights reserved.</p>
            <p>Contact us: <a href="mailto:holoquest@gmail.com">holoquest@gmail.com</a></p>
          </div>
        </footer>
      </div>
    </Router>
  );
}

export default App;
