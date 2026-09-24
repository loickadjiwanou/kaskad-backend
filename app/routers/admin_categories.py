"""Gestion des catégories : création, modification, réorganisation, réassignation d'apps, suppression."""

from fastapi import APIRouter

from app.core.i18n import ApiError
from app.deps import CurrentAdmin, Db
from app.models.common import maybe_oid, now, oid
from app.models.schemas import CategoryIn, CategoryOrder, CategoryReassign, CategoryUpdate
from app.services.activity import log_activity
from app.services.catalog import category_out

router = APIRouter(prefix="/admin/categories", tags=["admin: categories"])


async def _get(db, category_id: str) -> dict:
    cat = await db.categories.find_one({"_id": oid(category_id, "category_not_found")})
    if not cat:
        raise ApiError(404, "category_not_found")
    return cat


@router.get("")
async def list_categories(db: Db, _: CurrentAdmin):
    cats = await db.categories.find().sort([("order", 1), ("name", 1)]).to_list(None)
    counts = {
        r["_id"]: r["n"]
        for r in await (
            await db.apps.aggregate([{"$unwind": "$category_ids"}, {"$group": {"_id": "$category_ids", "n": {"$sum": 1}}}])
        ).to_list(None)
    }
    return [{**category_out(c), "apps_count": counts.get(c["_id"], 0)} for c in cats]


@router.post("", status_code=201)
async def create_category(db: Db, admin: CurrentAdmin, body: CategoryIn):
    order = body.order
    if order is None:
        last = await db.categories.find().sort("order", -1).to_list(1)
        order = (last[0]["order"] + 1) if last else 1
    doc = {"name": body.name, "icon": body.icon, "order": order, "created_at": now(), "updated_at": now()}
    doc["_id"] = (await db.categories.insert_one(doc)).inserted_id
    await log_activity(db, admin, "category.created", "category", doc["_id"], {"name": body.name})
    return category_out(doc)


@router.patch("/{category_id}")
async def update_category(db: Db, admin: CurrentAdmin, category_id: str, body: CategoryUpdate):
    cat = await _get(db, category_id)
    update = {**body.model_dump(exclude_none=True), "updated_at": now()}
    await db.categories.update_one({"_id": cat["_id"]}, {"$set": update})
    await log_activity(db, admin, "category.updated", "category", cat["_id"], body.model_dump(exclude_none=True))
    return category_out(await db.categories.find_one({"_id": cat["_id"]}))


@router.put("/order")
async def reorder_categories(db: Db, admin: CurrentAdmin, body: CategoryOrder):
    """Réorganisation : l'ordre de la liste d'IDs devient l'ordre d'affichage."""
    for index, cid in enumerate(body.ids, start=1):
        if o := maybe_oid(cid):
            await db.categories.update_one({"_id": o}, {"$set": {"order": index, "updated_at": now()}})
    await log_activity(db, admin, "category.reordered", "category", None, {"ids": body.ids})
    return await list_categories(db, admin)


async def _move_apps(db, source, target, app_ids=None) -> int:
    flt: dict = {"category_ids": source}
    if app_ids is not None:
        flt["_id"] = {"$in": app_ids}
    # Ajout de la nouvelle catégorie puis retrait de l'ancienne (évite les doublons)
    await db.apps.update_many(flt, {"$addToSet": {"category_ids": target}})
    res = await db.apps.update_many(flt, {"$pull": {"category_ids": source}, "$set": {"updated_at": now()}})
    return res.modified_count


@router.post("/{category_id}/reassign")
async def reassign_apps(db: Db, admin: CurrentAdmin, category_id: str, body: CategoryReassign):
    source = await _get(db, category_id)
    target = await _get(db, body.to_category_id)
    app_ids = [o for o in (maybe_oid(a) for a in body.app_ids)] if body.app_ids is not None else None
    moved = await _move_apps(db, source["_id"], target["_id"], app_ids)
    await log_activity(db, admin, "category.apps_reassigned", "category", source["_id"], {"to": str(target["_id"]), "moved": moved})
    return {"moved": moved}


@router.delete("/{category_id}", status_code=204)
async def delete_category(db: Db, admin: CurrentAdmin, category_id: str, reassign_to: str | None = None):
    """Supprime une catégorie. Si des apps l'utilisent, `reassign_to` est obligatoire (les apps y sont déplacées)."""
    cat = await _get(db, category_id)
    in_use = await db.apps.count_documents({"category_ids": cat["_id"]})
    if in_use:
        if not reassign_to:
            raise ApiError(409, "category_in_use")
        target = await _get(db, reassign_to)
        await _move_apps(db, cat["_id"], target["_id"])
    await db.categories.delete_one({"_id": cat["_id"]})
    await log_activity(db, admin, "category.deleted", "category", cat["_id"], {"name": cat["name"], "reassigned_to": reassign_to})
