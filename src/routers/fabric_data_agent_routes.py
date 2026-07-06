from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from authentication.dependencies import require_user, require_admin_user
from db.database import get_db
from schemas.fabric_data_agent import (
    FabricDataAgentCreate,
    FabricDataAgentRead,
    FabricDataAgentUpdate,
)
from services.fabric.fabric_data_agent_service import FabricDataAgentService

# Admin-gated: registering data agents + their credentials governs what data the
# orchestrator can reach. Reads require a logged-in user; writes require admin.
router = APIRouter(prefix="/admin/fabric-data-agents", tags=["fabric"])


@router.get("", response_model=list[FabricDataAgentRead])
def list_agents(db: Session = Depends(get_db), user=Depends(require_user)):
    return FabricDataAgentService(db).list()


@router.post("", response_model=FabricDataAgentRead, dependencies=[Depends(require_admin_user)])
def create_agent(body: FabricDataAgentCreate, db: Session = Depends(get_db)):
    try:
        return FabricDataAgentService(db).create(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{agent_id}", response_model=FabricDataAgentRead, dependencies=[Depends(require_admin_user)])
def update_agent(agent_id: int, body: FabricDataAgentUpdate, db: Session = Depends(get_db)):
    out = FabricDataAgentService(db).update(agent_id, body)
    if out is None:
        raise HTTPException(status_code=404, detail="Fabric data agent not found")
    return out


@router.delete("/{agent_id}", dependencies=[Depends(require_admin_user)])
def delete_agent(agent_id: int, db: Session = Depends(get_db)):
    if not FabricDataAgentService(db).delete(agent_id):
        raise HTTPException(status_code=404, detail="Fabric data agent not found")
    return {"deleted": True}


@router.post("/{agent_id}/test", dependencies=[Depends(require_admin_user)])
async def test_agent(agent_id: int, db: Session = Depends(get_db)):
    """Mint a token and ask a trivial question to verify connectivity + auth."""
    from services.fabric.fabric_data_agent_service import ask, get_token
    svc = FabricDataAgentService(db)
    agent = svc.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Fabric data agent not found")
    # First check auth alone (clearer error), then a tiny end-to-end query.
    try:
        await get_token(agent)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "auth", "error": str(e)}
    try:
        result = await ask(agent, "Reply with 'ok' to confirm you are reachable.")
        return {"ok": True, "stage": "query", "answer": result.get("answer"), "status": result.get("status")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "query", "error": str(e)}
