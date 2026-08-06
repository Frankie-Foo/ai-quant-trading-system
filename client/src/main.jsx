import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import AnalystApp from './AnalystApp'
import './analyst.css'
import './styles.css'

const RootApp = window.analystDesktop?.edition === 'macos-research'
  ? AnalystApp
  : App

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RootApp />
  </React.StrictMode>,
)
