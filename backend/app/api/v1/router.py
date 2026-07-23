from fastapi import APIRouter

from app.api.v1 import auth, cloud_accounts, files, subscriptions, system, tasks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(cloud_accounts.router)
api_router.include_router(subscriptions.router)
api_router.include_router(files.router)
api_router.include_router(tasks.router)
api_router.include_router(system.router)
