"""
services/providers/registry_service.py

CRUD + resolution for the provider/model registry. API keys are encrypted at rest
(Fernet via utils.crypto) and never returned in read models.

`resolve_slot()` maps a routing slot (e.g. "chat.planner") to a concrete
provider+model and is what the LLMRouter uses to dispatch.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.provider import CapabilityAssignment, Provider, ProviderModel
from schemas.provider import (
    CapabilityAssignmentRead,
    ProviderCreate,
    ProviderModelCreate,
    ProviderModelRead,
    ProviderModelUpdate,
    ProviderRead,
    ProviderUpdate,
    ResolvedModel,
)
from utils.crypto import decrypt_value, encrypt_value

log = get_logger(__name__)


class ProviderRegistryService:
    def __init__(self, db: Session):
        self.db = db

    # ── Providers ────────────────────────────────────────────────────────────
    def _to_read(self, p: Provider) -> ProviderRead:
        return ProviderRead(
            id=p.id,
            name=p.name,
            provider_type=p.provider_type,
            base_url=p.base_url,
            enabled=bool(p.enabled),
            is_local=bool(p.is_local),
            config_json=p.config_json,
            has_api_key=bool(p.api_key_encrypted),
            has_admin_key=bool(getattr(p, "admin_api_key_encrypted", None)),
            key_status=self._key_status(p),
        )

    @staticmethod
    def _key_status(p: Provider) -> str:
        """'none' (no key), 'ok' (decrypts), or 'unreadable' (stored but can't be
        decrypted — usually the encryption key changed since it was saved)."""
        if not p.api_key_encrypted:
            return "none"
        try:
            from utils.crypto import decrypt_value
            decrypt_value(p.api_key_encrypted)
            return "ok"
        except Exception:  # noqa: BLE001 — quiet check (no log spam on list)
            return "unreadable"

    def list_providers(self) -> List[ProviderRead]:
        return [self._to_read(p) for p in self.db.query(Provider).order_by(Provider.id).all()]

    def get_provider(self, provider_id: int) -> Optional[Provider]:
        return self.db.query(Provider).filter(Provider.id == provider_id).first()

    def create_provider(self, data: ProviderCreate) -> ProviderRead:
        obj = Provider(
            name=data.name,
            provider_type=data.provider_type,
            base_url=data.base_url,
            enabled=data.enabled,
            is_local=data.is_local,
            config_json=data.config_json,
            api_key_encrypted=encrypt_value(data.api_key) if data.api_key else None,
            admin_api_key_encrypted=encrypt_value(data.admin_api_key) if data.admin_api_key else None,
        )
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        log.infox("Provider aangemaakt", provider_id=obj.id, provider_type=obj.provider_type, has_api_key=bool(obj.api_key_encrypted))
        return self._to_read(obj)

    def update_provider(self, provider_id: int, data: ProviderUpdate) -> Optional[ProviderRead]:
        obj = self.get_provider(provider_id)
        if obj is None:
            return None
        fields = data.model_dump(exclude_unset=True)
        if "api_key" in fields:
            api_key = fields.pop("api_key")
            obj.api_key_encrypted = encrypt_value(api_key) if api_key else None
        if "admin_api_key" in fields:
            admin_key = fields.pop("admin_api_key")
            obj.admin_api_key_encrypted = encrypt_value(admin_key) if admin_key else None
        for k, v in fields.items():
            setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return self._to_read(obj)

    def delete_provider(self, provider_id: int) -> bool:
        obj = self.get_provider(provider_id)
        if obj is None:
            return False
        self.db.delete(obj)
        self.db.commit()
        return True

    def get_api_key(self, provider_id: int) -> Optional[str]:
        """Decrypt and return the plaintext API key (server-side use only)."""
        obj = self.get_provider(provider_id)
        if obj is None or not obj.api_key_encrypted:
            return None
        try:
            return decrypt_value(obj.api_key_encrypted)
        except Exception as exc:  # noqa: BLE001 — corrupt/rotated key must not crash callers
            log.warningx("Provider API key decrypt mislukt", provider_id=provider_id, error=str(exc))
            return None

    def get_admin_api_key(self, provider_id: int) -> Optional[str]:
        """Decrypt and return the plaintext Admin/usage key (server-side use only)."""
        obj = self.get_provider(provider_id)
        enc = getattr(obj, "admin_api_key_encrypted", None) if obj else None
        if not enc:
            return None
        try:
            return decrypt_value(enc)
        except Exception as exc:  # noqa: BLE001 — corrupt/rotated key must not crash callers
            log.warningx("Provider admin key decrypt mislukt", provider_id=provider_id, error=str(exc))
            return None

    # ── Provider models ──────────────────────────────────────────────────────
    @staticmethod
    def _model_to_read(m: ProviderModel) -> ProviderModelRead:
        from services.providers.web_search_capability import effective_web_search
        from services.providers.vision_capability import effective_vision
        ptype = m.provider.provider_type if getattr(m, "provider", None) else None
        return ProviderModelRead(
            id=m.id,
            provider_id=m.provider_id,
            model_id=m.model_id,
            capability=m.capability,
            display_name=m.display_name,
            context_window=m.context_window,
            price_in=m.price_in,
            price_out=m.price_out,
            good_for=m.good_for,
            enabled=bool(m.enabled),
            is_local=bool(m.is_local),
            deploy_state=m.deploy_state,
            supports_web_search=m.supports_web_search,
            web_search_capable=effective_web_search(ptype, m.model_id, m.supports_web_search),
            supports_vision=m.supports_vision,
            vision_capable=effective_vision(ptype, m.model_id, m.supports_vision),
            needs_extra_guidance=m.needs_extra_guidance,
            prompt_mode=m.prompt_mode,
            num_parallel=m.num_parallel,
        )

    def list_models(self, *, provider_id: Optional[int] = None, capability: Optional[str] = None) -> List[ProviderModelRead]:
        q = self.db.query(ProviderModel)
        if provider_id is not None:
            q = q.filter(ProviderModel.provider_id == provider_id)
        if capability is not None:
            q = q.filter(ProviderModel.capability == capability)
        return [self._model_to_read(m) for m in q.order_by(ProviderModel.id).all()]

    def create_model(self, data: ProviderModelCreate) -> ProviderModelRead:
        obj = ProviderModel(**data.model_dump())
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return self._model_to_read(obj)

    def update_model(self, model_pk: int, data: ProviderModelUpdate) -> Optional[ProviderModelRead]:
        obj = self.db.query(ProviderModel).filter(ProviderModel.id == model_pk).first()
        if obj is None:
            return None
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return self._model_to_read(obj)

    def delete_model(self, model_pk: int) -> bool:
        obj = self.db.query(ProviderModel).filter(ProviderModel.id == model_pk).first()
        if obj is None:
            return False
        self.db.delete(obj)
        self.db.commit()
        return True

    # ── Capability assignments ───────────────────────────────────────────────
    @staticmethod
    def _execution_mode(provider_type: Optional[str], assigned: bool) -> Optional[str]:
        """'agent' for a CLI-agent provider, 'model' for a plain one, None when the
        slot is unassigned (step off — the no-fallback rule). For the UI badge."""
        if not assigned:
            return None
        from services.providers.execution_mode import is_cli_agent_type
        return "agent" if is_cli_agent_type(provider_type) else "model"

    def list_assignments(self) -> List[CapabilityAssignmentRead]:
        out: List[CapabilityAssignmentRead] = []
        for a in self.db.query(CapabilityAssignment).order_by(CapabilityAssignment.slot).all():
            pm = a.provider_model
            ptype = (pm.provider.provider_type if pm and pm.provider else None)
            out.append(CapabilityAssignmentRead(
                slot=a.slot,
                provider_model_id=a.provider_model_id,
                provider_type=ptype,
                model_id=(pm.model_id if pm else None),
                execution_mode=self._execution_mode(ptype, a.provider_model_id is not None),
            ))
        return out

    def set_assignment(self, slot: str, provider_model_id: Optional[int]) -> CapabilityAssignmentRead:
        # Only canonical slots may be assigned — stale rows for removed slots
        # (chat.router / chat.final_answer / chat.memory_decision) confused
        # operators into configuring models that were never used.
        from services.providers.capability_router import ALL_SLOTS
        if slot not in ALL_SLOTS:
            raise ValueError(f"Unknown routing slot '{slot}'. Valid slots: {', '.join(ALL_SLOTS)}")
        # No-fallback rule + modality guard: a CLI-agent provider runs its own agent
        # loop and has no interface for modality/realtime work (TTS/STT/live/
        # embeddings/image). Reject it at ASSIGNMENT time rather than silently
        # substituting another model at runtime.
        if provider_model_id is not None:
            from services.providers.execution_mode import MODALITY, capability_class, is_cli_agent_type
            if capability_class(slot) == MODALITY:
                from models.provider import ProviderModel
                pm_row = self.db.query(ProviderModel).filter(ProviderModel.id == provider_model_id).first()
                ptype = (pm_row.provider.provider_type if pm_row and pm_row.provider else None)
                if is_cli_agent_type(ptype):
                    raise ValueError(
                        f"A CLI-agent provider (e.g. Claude Code) cannot be assigned to the "
                        f"modality slot '{slot}': TTS/STT/live/embeddings/image run in the "
                        f"orchestrator only. Assign a normal model provider here.")
        obj = self.db.query(CapabilityAssignment).filter(CapabilityAssignment.slot == slot).first()
        if obj is None:
            obj = CapabilityAssignment(slot=slot, provider_model_id=provider_model_id)
            self.db.add(obj)
        else:
            obj.provider_model_id = provider_model_id
        self.db.commit()
        self.db.refresh(obj)
        pm = obj.provider_model
        ptype = (pm.provider.provider_type if pm and pm.provider else None)
        return CapabilityAssignmentRead(
            slot=obj.slot,
            provider_model_id=obj.provider_model_id,
            provider_type=ptype,
            model_id=(pm.model_id if pm else None),
            execution_mode=self._execution_mode(ptype, obj.provider_model_id is not None),
        )

    def model_needs_extra_guidance(self, model_id: Optional[str]) -> bool:
        """True if any enabled provider-model with this model_id is flagged
        needs_extra_guidance (AI Models → Routing per-model toggle). Resolves the
        concrete model used by the agent for the current turn."""
        mid = (model_id or "").strip()
        if not mid:
            return False
        try:
            rows = (
                self.db.query(ProviderModel)
                .filter(ProviderModel.model_id == mid,
                        ProviderModel.needs_extra_guidance.is_(True))
                .all()
            )
            return any(bool(r.needs_extra_guidance) for r in rows)
        except Exception:  # noqa: BLE001 — never break the turn on a lookup
            return False

    def model_is_vision_capable(self, model_id: Optional[str]) -> bool:
        """Effective vision capability for a model id (override OR curated)."""
        mid = (model_id or "").strip()
        if not mid:
            return False
        try:
            from services.providers.vision_capability import effective_vision
            rows = self.db.query(ProviderModel).filter(ProviderModel.model_id == mid).all()
            for r in rows:
                p = r.provider
                if p is not None and p.enabled and r.enabled and effective_vision(
                    p.provider_type, r.model_id, r.supports_vision
                ):
                    return True
            return False
        except Exception:  # noqa: BLE001 — never break the turn on a lookup
            return False

    def resolve_vision_model(self, active_model: Optional[str] = None) -> Optional[str]:
        """The model to use for LOOKING at images this turn: the active chat
        model when it is vision-capable, else the planner-slot model when
        capable, else ANY enabled vision-capable chat model (cloud or local).
        None → nothing in the workspace can see images."""
        if active_model and self.model_is_vision_capable(active_model):
            return active_model.strip()
        try:
            r = self.resolve_slot("chat.planner")
            slot_model = getattr(r, "model_id", None) if r else None
            if slot_model and self.model_is_vision_capable(slot_model):
                return slot_model
            from services.providers.vision_capability import effective_vision
            for m in self.db.query(ProviderModel).filter(
                ProviderModel.capability == "chat", ProviderModel.enabled.is_(True)
            ).order_by(ProviderModel.id).all():
                p = m.provider
                if p is not None and p.enabled and effective_vision(p.provider_type, m.model_id, m.supports_vision):
                    return m.model_id
            return None
        except Exception:  # noqa: BLE001
            return None

    def model_prompt_light(self, model_id: Optional[str]) -> bool:
        """Effective planner prompt mode for this model: explicit per-model
        'light'/'full' wins; unset (auto) → light when the model is local.
        Small local models are prefill-bound, so the compact prompt is the
        sensible default for them."""
        mid = (model_id or "").strip()
        if not mid:
            return False
        try:
            rows = self.db.query(ProviderModel).filter(ProviderModel.model_id == mid).all()
            for r in rows:
                mode = (r.prompt_mode or "").strip().lower()
                if mode == "light":
                    return True
                if mode == "full":
                    return False
            return self.model_is_local(mid)
        except Exception:  # noqa: BLE001 — never break the turn on a lookup
            return False

    def model_num_parallel(self, model_id: Optional[str]) -> int:
        """Configured concurrent-turn threshold for a local model (queue
        indicator). Max over rows with this id; at least 1."""
        mid = (model_id or "").strip()
        if not mid:
            return 1
        try:
            rows = self.db.query(ProviderModel).filter(ProviderModel.model_id == mid).all()
            best = 1
            for r in rows:
                v = int(r.num_parallel) if r.num_parallel else 0
                if v > best:
                    best = v
            return best
        except Exception:  # noqa: BLE001
            return 1

    def model_is_local(self, model_id: Optional[str]) -> bool:
        """True if this model_id belongs to an enabled LOCAL provider (or is a
        local-flagged model row). Used to give local turns a bigger runtime
        budget — local planner steps are far slower than cloud ones."""
        mid = (model_id or "").strip()
        if not mid:
            return False
        try:
            rows = self.db.query(ProviderModel).filter(ProviderModel.model_id == mid).all()
            for r in rows:
                p = r.provider
                if p is not None and p.enabled and (bool(p.is_local) or bool(r.is_local)):
                    return True
            return False
        except Exception:  # noqa: BLE001 — never break the turn on a lookup
            return False

    def resolve_slot(self, slot: str) -> Optional[ResolvedModel]:
        """Resolve a routing slot to a concrete enabled provider+model, or None."""
        a = self.db.query(CapabilityAssignment).filter(CapabilityAssignment.slot == slot).first()
        if a is None or a.provider_model_id is None:
            return None
        pm = a.provider_model
        if pm is None or not pm.enabled:
            return None
        p = pm.provider
        if p is None or not p.enabled:
            return None
        return ResolvedModel(
            slot=slot,
            provider_id=p.id,
            provider_type=p.provider_type,
            base_url=p.base_url,
            model_id=pm.model_id,
            capability=pm.capability,
            has_api_key=bool(p.api_key_encrypted),
        )
