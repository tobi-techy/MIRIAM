import os
import sys
from pathlib import Path

# Add current directory to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

def main():
    """Main CLI entry point for Miriam Financial Agent"""
    import uvicorn
    from miriam_agent.api.main import app
    
    # Get configuration from environment
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("ENVIRONMENT", "production").lower() == "development"
    
    print(f"Starting Miriam Financial Agent on {host}:{port}")
    print(f"Environment: {os.getenv('ENVIRONMENT', 'production')}")
    print(f"Reload mode: {reload}")
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level=os.getenv("LOG_LEVEL", "info").lower()
    )

if __name__ == "__main__":
    main()
