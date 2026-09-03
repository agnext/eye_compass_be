# Eye Compass Frontend: Base Architecture & Setup Walkthrough

This document records the initial setup and architectural decisions made during Phase 1 of the frontend segregation (migrating from PyQt to a React PWA).

## 1. Core Scaffolding
- **Tooling:** We initialized the project using `Vite` with the `react` template for rapid development and optimized builds.
- **Dependencies Installed:** 
  - `react-router-dom`: For client-side routing between the Dashboard and Login screens.
  - `vite-plugin-pwa`: For generating the service workers and web manifests required to run the app in Kiosk mode.

## 2. The Design System (Vanilla CSS)
- **`src/styles/variables.css`**: We defined a strict set of CSS Custom Properties tailored to a "Premium Industrial Edge" aesthetic. This includes:
  - Dark background base (`bg-primary`).
  - Cyan and neon red accent colors with glow effects for active/inactive states.
  - Glassmorphism backdrop filters for floating panels.
- **`src/styles/global.css`**: Created global utility classes (like `.glass-panel` and `.btn-neon`) that can be applied to any component. We chose Vanilla CSS over Tailwind to maintain absolute control over the exact pixels and avoid HTML clutter in a highly customized hardware display.

## 3. Component Architecture
- **`src/layouts/MainLayout.jsx`**: The core "shell" of the application. 
  - Includes a top status bar displaying hardware connection status and the active operator.
  - Provides a flexible grid area for injecting the main content.
- **`src/pages/Dashboard.jsx`**: The primary operator view. 
  - Contains a highly visible central container for the future Camera Feed (currently displaying a placeholder).
  - Includes a sidebar for starting/stopping the machine learning models and viewing live stats.

## 4. Progressive Web App (PWA) Configuration
- **`vite.config.js`**: We configured `vite-plugin-pwa` with the following critical settings:
  - `display: 'standalone'`: This is the most important setting. It ensures that when the Jetson device boots into Chromium, the app runs in full-screen Kiosk mode without any browser toolbars or URL bars, mimicking the native feel of the old PyQt app.
  - `theme_color` & `background_color`: Set to our dark `#0A0A0C` to ensure seamless loading screens.

## 5. Phase 2 Features Additions
- **`src/pages/Login.jsx`**: 
   - Authenticates against the FastAPI backend. 
   - Supports offline login (credentials match the backend `.env` / legacy `config.INI`).
- **`src/pages/Home.jsx`**: 
   - Replaces the legacy main menu.
   - Provides clear navigation blocks for "New Batch" (Data Collection), "History", and "Settings".
- **`src/pages/DetailsEntry.jsx` (Data Collection)**: 
   - Replicates the legacy Data Collection entry form.
   - **Dynamic Dropdowns:** On mount, it calls `GET /api/config/commodities`. When the user selects a "Commodity", the UI instantly filters and populates the "Variety" and "FM" (Foreign Matter) dropdowns based on the cached config fetched from Qualix.
   - Includes controls to jog the conveyor forward/backward for testing before a scan.
