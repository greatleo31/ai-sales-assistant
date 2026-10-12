import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './styles/ios27-glass.css';

const rootEl = document.getElementById('root');
createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
