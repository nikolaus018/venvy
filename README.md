# Venvy

Venvy is a powerful, local web-based environment manager for Python and Rust applications. It provides a polished, modern dashboard to effortlessly manage, deploy, and monitor your local applications, acting as a complete development and local-hosting solution.

![Dashboard Preview](https://via.placeholder.com/800x400?text=Venvy+Dashboard)

## Features

- 🚀 **Web-Based Dashboard**: A premium, glassmorphic UI to manage all your applications in one place.
- 🌐 **Cloudflare Tunnel Integration**: Instantly expose your local applications to the public internet securely.
- 📁 **Integrated IDE**: Fully-featured file explorer and integrated Ace/Monaco-powered code editor directly in the browser.
- 💻 **Built-in Terminal**: A fully functional, persistent `xterm.js` terminal for direct shell access to your app environments.
- 📦 **Dependency Management**: Manage Python packages and dependencies without leaving the UI.
- 📊 **System Monitoring**: Real-time insights into host CPU, memory usage, and port allocations.
- 🔒 **Security First**: Role-based access control (Admin/User), TOTP two-factor authentication, and safe DOM manipulation.

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js (Optional, if you wish to run JavaScript applications alongside via extensions)
- (Optional) Cloudflare `cloudflared` installed if using external tunneling.

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/venvy.git
   cd venvy
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   fastapi dev app/main.py
   # OR
   python3 main.py
   ```

4. Access the dashboard:
   Open your browser and navigate to `http://localhost:8000`.
   On your first run, a default admin account setup screen will be presented.

## Technology Stack

- **Backend**: FastAPI, SQLModel (SQLite), Uvicorn, psutil
- **Frontend**: Vanilla JS, HTML5, TailwindCSS (for rapid prototyping), xterm.js, Ace Editor
- **Architecture**: Asynchronous event loop, subprocess management, and WebSocket-driven terminal streaming.

## Security Overview

Venvy emphasizes security:
- No raw `innerHTML` execution paths for user-provided data.
- Strict dependency management using precise version pinning.
- Isolated subprocess launches.
- Hardened authentication flows preventing CSRF and XSS.

## Contributing

We welcome contributions! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

## License

MIT License. See `LICENSE` for more information.
