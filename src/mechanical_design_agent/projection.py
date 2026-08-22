from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .migrations import neo4j_migrations_directory
from .repository import PostgresRepository


class Neo4jProjection:
    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.user = user
        self.password = password

    def _driver(self):
        from neo4j import GraphDatabase

        return GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def status(self) -> dict[str, Any]:
        try:
            with self._driver() as driver:
                driver.verify_connectivity()
                with driver.session() as session:
                    version = session.run(
                        "CALL dbms.components() YIELD name, versions RETURN name, versions LIMIT 1"
                    ).single()
            return {"status": "healthy", "component": dict(version) if version else {}}
        except Exception as exc:
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    def scoped_relationships(
        self, *, family_id: str | None, model_revision_id: str | None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Read only graph edges already authorized by the design-context gate."""
        if not family_id and not model_revision_id:
            return []
        if not 1 <= limit <= 500:
            raise ValueError("graph relationship limit must be between 1 and 500")
        owner = "freecad-mechanical-design-agent"
        rows: list[dict[str, Any]] = []
        with self._driver() as driver, driver.session() as session:
            if model_revision_id:
                result = session.run(
                    "MATCH (m:ModelRevision {id:$model_id,projection_owner:$owner})-[r]->(n) "
                    "WHERE n.projection_owner=$owner "
                    "RETURN labels(m) AS source_labels,m.id AS source_id,type(r) AS relationship,"
                    "labels(n) AS target_labels,coalesce(n.id,n.key) AS target_id "
                    "ORDER BY relationship,target_id LIMIT $limit",
                    model_id=model_revision_id,
                    owner=owner,
                    limit=limit,
                )
                rows.extend(dict(record) for record in result)
            if family_id:
                remaining = max(0, limit - len(rows))
                if remaining:
                    result = session.run(
                        "MATCH (n)-[r]->(f:ProductFamily {id:$family_id,projection_owner:$owner}) "
                        "WHERE n.projection_owner=$owner AND (NOT ('KnowledgeAssertion' IN labels(n)) OR n.status='approved') "
                        "RETURN labels(n) AS source_labels,coalesce(n.id,n.key) AS source_id,"
                        "type(r) AS relationship,labels(f) AS target_labels,f.id AS target_id "
                        "ORDER BY relationship,source_id LIMIT $limit",
                        family_id=family_id,
                        owner=owner,
                        limit=remaining,
                    )
                    rows.extend(dict(record) for record in result)
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("source_id")), str(row.get("relationship")), str(row.get("target_id")))
            unique[key] = row
        return list(unique.values())

    def initialize_constraints(self) -> None:
        with neo4j_migrations_directory() as migrations:
            statements = [
                item.strip()
                for path in sorted(migrations.glob("*.cypher"))
                for item in path.read_text(encoding="utf-8").split(";")
                if item.strip()
            ]
        with self._driver() as driver, driver.session() as session:
            for statement in statements:
                session.run(statement).consume()

    def constraint_names(self) -> list[str]:
        with self._driver() as driver, driver.session() as session:
            return [
                str(record["name"])
                for record in session.run(
                    "SHOW CONSTRAINTS YIELD name RETURN name ORDER BY name"
                )
            ]

    def rebuild(self, repository: PostgresRepository) -> dict[str, Any]:
        """Atomically replace the owned graph with one validated PostgreSQL replay."""
        self.initialize_constraints()
        generation = str(uuid4())
        with self._driver() as driver, driver.session() as session:
            counts = session.execute_write(
                lambda transaction: self._rebuild_transaction(
                    transaction,
                    repository,
                    generation=generation,
                )
            )
        return {
            "status": "rebuilt-from-postgresql",
            "authoritative_source": "postgresql",
            "active_generation": generation,
            "counts": counts,
        }

    def _rebuild_transaction(
        self,
        transaction: Any,
        repository: PostgresRepository,
        *,
        generation: str,
    ) -> dict[str, int]:
        owner = "freecad-mechanical-design-agent"
        counts = {
            "families": 0,
            "subfamilies": 0,
            "products": 0,
            "models": 0,
            "assertions": 0,
            "design_lessons": 0,
            "design_lesson_reviews": 0,
            "profiles": 0,
        }
        self._acquire_projection_lock(transaction)
        transaction.run(
            "MATCH (n) WHERE n.projection_owner=$owner AND NOT n:ProjectionState "
            "DETACH DELETE n",
            owner=owner,
        ).consume()
        for family in repository.projection_families():
            self._project_family(transaction, family)
            counts["families"] += 1
        for product in repository.projection_products():
            self._project_product(transaction, product)
            counts["products"] += 1
        for subfamily in repository.projection_subfamilies():
            self._project_subfamily(transaction, subfamily)
            counts["subfamilies"] += 1
        for model in repository.projection_models():
            self._project_model(transaction, model)
            counts["models"] += 1
        for assertion in repository.projection_assertions():
            self._project_assertion(transaction, assertion)
            counts["assertions"] += 1
        for lesson in repository.projection_design_lessons():
            self._project_design_lesson(transaction, lesson)
            counts["design_lessons"] += 1
        for review in repository.projection_design_lesson_reviews():
            self._project_design_lesson_review(
                transaction,
                review,
                occurred_at=review.get("occurred_at"),
                aggregate_version=int(review.get("aggregate_version") or 0),
            )
            counts["design_lesson_reviews"] += 1
        for profile in repository.projection_profiles():
            self._project_profile(transaction, profile)
            counts["profiles"] += 1
        transaction.run(
            "MATCH (n) WHERE n.projection_owner=$owner "
            "SET n.projection_generation=$generation",
            owner=owner,
            generation=generation,
        ).consume()
        self._validate_generation(transaction, generation=generation, counts=counts)
        transaction.run(
            "MERGE (state:ProjectionState {name:$state_name}) "
            "SET state.active_generation=$generation,state.projection_owner=$owner",
            state_name="mechanical-design-agent",
            generation=generation,
            owner=owner,
        ).consume()
        return counts

    @staticmethod
    def _acquire_projection_lock(transaction: Any) -> None:
        """Serialize rebuild and incremental writes on one constrained state node."""
        transaction.run(
            "MERGE (state:ProjectionState {name:$state_name}) "
            "SET state.projection_epoch=coalesce(state.projection_epoch,0)+1,"
            "state.projection_owner=$owner "
            "RETURN state.projection_epoch AS projection_epoch",
            state_name="mechanical-design-agent",
            owner="freecad-mechanical-design-agent",
        ).consume()

    @staticmethod
    def _validate_generation(
        transaction: Any,
        *,
        generation: str,
        counts: dict[str, int],
    ) -> None:
        owner = "freecad-mechanical-design-agent"
        labels = {
            "families": "ProductFamily",
            "subfamilies": "ProductSubfamily",
            "products": "Product",
            "models": "ModelRevision",
            "assertions": "KnowledgeAssertion",
            "design_lessons": "DesignLesson",
            "design_lesson_reviews": "DesignLessonReview",
            "profiles": "FamilyProfile",
        }
        for count_name, label in labels.items():
            expected = counts[count_name]
            record = transaction.run(
                f"MATCH (n:{label}) "
                "WHERE n.projection_owner=$owner AND n.projection_generation=$generation "
                "RETURN count(n) AS node_count",
                owner=owner,
                generation=generation,
                expected_count=expected,
            ).single()
            actual = int(record["node_count"]) if record is not None else -1
            if actual != expected:
                raise RuntimeError(
                    f"Neo4j rebuild validation failed for {count_name}: "
                    f"expected {expected}, found {actual}"
                )

    def project_pending(self, repository: PostgresRepository, limit: int = 100) -> dict[str, Any]:
        successes = 0
        failures = []
        processed_events: list[dict[str, str]] = []
        self.initialize_constraints()
        worker_id = f"neo4j-projection-{uuid4()}"
        events = repository.claim_outbox(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=60,
        )
        with self._driver() as driver:
            for event in events:
                try:
                    with driver.session() as session:
                        session.execute_write(
                            lambda transaction: self._project_claimed_event(
                                transaction, repository, event
                            )
                        )
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    try:
                        repository.mark_outbox(
                            str(event["id"]),
                            worker_id=worker_id,
                            error=message,
                        )
                    except Exception as acknowledgement_error:
                        message += (
                            "; acknowledgement lost: "
                            f"{type(acknowledgement_error).__name__}: {acknowledgement_error}"
                        )
                    failures.append({"event_id": str(event["id"]), "error": message})
                    continue
                try:
                    repository.mark_outbox(str(event["id"]), worker_id=worker_id)
                except Exception as exc:
                    failures.append({
                        "event_id": str(event["id"]),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
                successes += 1
                processed_event = {
                    "event_type": str(event["event_type"]),
                    "aggregate_type": str(event["aggregate_type"]),
                    "aggregate_id": str(event["aggregate_id"]),
                }
                if event.get("id") is not None:
                    processed_event["event_id"] = str(event["id"])
                processed_events.append(processed_event)
        return {
            "processed": successes,
            "failed": failures,
            "remaining_hint": max(0, len(events) - successes),
            "processed_events": processed_events,
        }

    def _project_claimed_event(
        self,
        transaction: Any,
        repository: PostgresRepository,
        event: dict[str, Any],
    ) -> None:
        self._acquire_projection_lock(transaction)
        self._project_event(transaction, repository, event)

    def _project_event(self, session: Any, repository: PostgresRepository, event: dict[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type.startswith("product_family."):
            self._project_family(session, repository.get_family(payload["family_id"]))
            return
        if event_type == "model_revision.analyzed":
            self._project_model(session, repository.get_model_analysis(payload["model_revision_id"]))
            return
        if event_type == "knowledge_assertion.reviewed":
            self._project_assertion(
                session,
                {
                    **repository.get_assertion(payload["assertion_id"]),
                    "aggregate_version": int(event["aggregate_version"]),
                },
            )
            return
        if event_type in {
            "design_lesson.approved",
            "design_lesson.superseded",
            "design_lesson.revoked",
        }:
            lesson_id = str(payload.get("lesson_id") or payload["design_lesson_id"])
            lesson = next(
                item for item in repository.projection_design_lessons() if str(item["id"]) == lesson_id
            )
            self._project_design_lesson(
                session,
                {**lesson, "aggregate_version": int(event["aggregate_version"])},
            )
            return
        if event_type in {
            "design_lesson_review.prepared",
            "design_lesson_review.superseded",
            "design_lesson_review.approved",
            "design_lesson_review.rejected",
            "design_lesson_review.invalid",
            "design_lesson_review.retrieval_verified",
        }:
            self._project_design_lesson_review(
                session,
                payload,
                occurred_at=(
                    event.get("projection_occurred_at")
                    or event.get("created_at")
                    or payload.get("occurred_at")
                ),
                aggregate_version=int(event["aggregate_version"]),
            )
            return
        if event_type == "family_profile.reviewed":
            profile = repository.get_family_profile_by_id(payload["profile_id"])
            self._project_profile(session, profile)
            return
        if event_type == "model_identity.confirmed":
            self._project_product(session, repository.get_product(payload["product_id"]))
            self._project_model(session, repository.get_model_analysis(payload["model_revision_id"]))
            return
        if event_type == "product_subfamily.reviewed":
            subfamily = repository.get_subfamily(payload["subfamily_id"])
            assignments = repository.family_subfamilies(subfamily["family_id"])
            matched = next(item for item in assignments if item["id"] == subfamily["id"])
            subfamily["model_revision_ids"] = [
                item["model_revision_id"]
                for item in matched["assignments"]
                if item["status"] == "approved"
            ]
            self._project_subfamily(session, subfamily)
            return
        if event_type == "design_working_copy.approved":
            # Delivery approval is authoritative PostgreSQL workflow state; it has no graph projection.
            return
        raise ValueError(f"unsupported outbox event type: {event_type}")

    @staticmethod
    def _project_family(session: Any, family: dict[str, Any]) -> None:
        session.run(
            "MERGE (f:ProductFamily {id:$id}) SET f.name=$name,f.status=$status,"
            "f.design_group_id=$design_group_id,f.projection_owner=$owner",
            id=family["id"],
            name=family["canonical_name"],
            status=family["status"],
            design_group_id=family["design_group_id"],
            owner="freecad-mechanical-design-agent",
        ).consume()

    @staticmethod
    def _project_product(session: Any, product: dict[str, Any]) -> None:
        owner = "freecad-mechanical-design-agent"
        session.run(
            "MERGE (p:Product {id:$id}) SET p.name=$name,p.status=$status,p.family_id=$family_id,"
            "p.design_group_id=$design_group_id,p.projection_owner=$owner",
            id=str(product["id"]),
            name=product["canonical_name"],
            status=product["status"],
            family_id=product.get("family_id"),
            design_group_id=product["design_group_id"],
            owner=owner,
        ).consume()
        if product.get("family_id"):
            session.run(
                "MATCH (p:Product {id:$product_id}) MERGE (f:ProductFamily {id:$family_id}) "
                "ON CREATE SET f.projection_owner=$owner MERGE (p)-[:MEMBER_OF]->(f)",
                product_id=str(product["id"]),
                family_id=product["family_id"],
                owner=owner,
            ).consume()

    @staticmethod
    def _project_subfamily(session: Any, subfamily: dict[str, Any]) -> None:
        owner = "freecad-mechanical-design-agent"
        session.run(
            "MERGE (s:ProductSubfamily {id:$id}) SET s.name=$name,s.status=$status,s.family_id=$family_id,"
            "s.projection_owner=$owner",
            id=subfamily["id"],
            name=subfamily["canonical_name"],
            status=subfamily["status"],
            family_id=subfamily["family_id"],
            owner=owner,
        ).consume()
        session.run(
            "MATCH (s:ProductSubfamily {id:$subfamily_id}) MERGE (f:ProductFamily {id:$family_id}) "
            "ON CREATE SET f.projection_owner=$owner MERGE (s)-[:SUBFAMILY_OF]->(f)",
            subfamily_id=subfamily["id"],
            family_id=subfamily["family_id"],
            owner=owner,
        ).consume()
        for model_id in subfamily.get("model_revision_ids", []):
            session.run(
                "MATCH (s:ProductSubfamily {id:$subfamily_id}) MERGE (m:ModelRevision {id:$model_id}) "
                "ON CREATE SET m.projection_owner=$owner MERGE (m)-[:CLASSIFIED_AS]->(s)",
                subfamily_id=subfamily["id"],
                model_id=str(model_id),
                owner=owner,
            ).consume()

    @staticmethod
    def _project_model(session: Any, model: dict[str, Any]) -> None:
        owner = "freecad-mechanical-design-agent"
        manifest = model["manifest"]
        session.run(
                "MERGE (m:ModelRevision {id:$id}) SET m.source_path=$source_path,m.status=$status,"
                "m.family_id=$family_id,m.projection_owner=$owner",
                id=str(model["id"]),
                source_path=model["source_relative_path"],
                status=model["status"],
                family_id=model.get("family_id"),
                owner=owner,
        ).consume()
        if model.get("family_id"):
            session.run(
                    "MATCH (m:ModelRevision {id:$model_id}) MERGE (f:ProductFamily {id:$family_id}) "
                    "ON CREATE SET f.projection_owner=$owner MERGE (m)-[:MEMBER_OF]->(f)",
                    model_id=str(model["id"]),
                    family_id=model["family_id"],
                    owner=owner,
            ).consume()
        if model.get("product_id"):
            session.run(
                "MATCH (m:ModelRevision {id:$model_id}) MERGE (p:Product {id:$product_id}) "
                "ON CREATE SET p.projection_owner=$owner MERGE (m)-[:REVISION_OF]->(p)",
                model_id=str(model["id"]),
                product_id=str(model["product_id"]),
                owner=owner,
            ).consume()
        for node in manifest.get("source_nodes", []):
            key = f"{model['id']}:{node['source_id']}"
            session.run(
                    "MATCH (m:ModelRevision {id:$model_id}) MERGE (n:SourceNode {key:$key}) "
                    "SET n.source_id=$source_id,n.name=$name,n.label=$label,n.kind=$kind,"
                    "n.projection_owner=$owner MERGE (m)-[:CONTAINS_SOURCE_NODE]->(n)",
                    model_id=str(model["id"]),
                    key=key,
                    source_id=node["source_id"],
                    name=node["source_name"],
                    label=node["source_label"],
                    kind=node["node_kind"],
                    owner=owner,
            ).consume()
        for node in manifest.get("source_nodes", []):
            parent = node.get("primary_parent_source_id")
            if parent:
                session.run(
                        "MATCH (child:SourceNode {key:$child_key}),(parent:SourceNode {key:$parent_key}) "
                        "MERGE (parent)-[:SOURCE_CONTAINS]->(child)",
                        child_key=f"{model['id']}:{node['source_id']}",
                        parent_key=f"{model['id']}:{parent}",
                ).consume()

    @staticmethod
    def _project_assertion(
        session: Any,
        assertion: dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        owner = "freecad-mechanical-design-agent"
        aggregate_version = int(assertion.get("aggregate_version", 0))
        applied = session.run(
                "MERGE (a:KnowledgeAssertion {id:$id}) "
                "ON CREATE SET a.aggregate_version=-1 "
                "WITH a WHERE $force OR coalesce(a.aggregate_version,-1) < $aggregate_version "
                "SET a.subject_ref=$subject_ref,a.predicate=$predicate,"
                "a.status=$status,a.scope_kind=$scope_kind,a.risk_level=$risk_level,a.family_id=$family_id,"
                "a.aggregate_version=CASE "
                "WHEN coalesce(a.aggregate_version,-1) > $aggregate_version "
                "THEN a.aggregate_version ELSE $aggregate_version END,"
                "a.projection_owner=$owner RETURN true AS applied",
                id=str(assertion["id"]),
                subject_ref=assertion["subject_ref"],
                predicate=assertion["predicate"],
                status=assertion["status"],
                scope_kind=assertion["scope_kind"],
                risk_level=assertion["risk_level"],
                family_id=assertion.get("family_id"),
                aggregate_version=aggregate_version,
                force=force,
                owner=owner,
        ).single()
        if applied is None or not bool(applied["applied"]):
            return False
        if assertion.get("family_id"):
            session.run(
                    "MATCH (a:KnowledgeAssertion {id:$assertion_id}) MERGE (f:ProductFamily {id:$family_id}) "
                    "ON CREATE SET f.projection_owner=$owner MERGE (a)-[:SCOPED_TO]->(f)",
                    assertion_id=str(assertion["id"]),
                    family_id=assertion["family_id"],
                    owner=owner,
            ).consume()
        return True

    @staticmethod
    def lesson_relationships(lesson: dict[str, Any]) -> list[dict[str, str]]:
        """Return immutable lesson provenance edges from one authoritative lesson row.

        These historical edges are idempotently merged and are never deleted
        because a lesson or linked assertion becomes inactive. Source-family
        provenance intentionally has no relationship here: it is not scope or
        authorization data.
        """
        relationships = [
            {
                "type": "GENERATED_ASSERTION",
                "target_label": "KnowledgeAssertion",
                "target_id": str(assertion["id"]),
            }
            for assertion in sorted(lesson.get("assertions", []), key=lambda item: str(item["id"]))
        ]
        if lesson.get("source_model_revision_id"):
            relationships.append(
                {
                    "type": "ORIGINATED_FROM",
                    "target_label": "ModelRevision",
                    "target_id": str(lesson["source_model_revision_id"]),
                }
            )
        if lesson.get("supersedes"):
            relationships.append(
                {
                    "type": "SUPERSEDES",
                    "target_label": "DesignLesson",
                    "target_id": str(lesson["supersedes"]),
                }
            )
        return relationships

    @staticmethod
    def _project_design_lesson(
        session: Any,
        lesson: dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        owner = "freecad-mechanical-design-agent"
        aggregate_version = int(lesson.get("aggregate_version", 0))
        applied = session.run(
            "MERGE (l:DesignLesson {id:$id}) "
            "ON CREATE SET l.aggregate_version=-1 "
            "WITH l WHERE $force OR coalesce(l.aggregate_version,-1) < $aggregate_version "
            "SET l.lesson_key=$lesson_key,l.title=$title,l.status=$status,"
            "l.organization_id=$organization_id,l.package_sha256=$package_sha256,"
            "l.aggregate_version=CASE "
            "WHEN coalesce(l.aggregate_version,-1) > $aggregate_version "
            "THEN l.aggregate_version ELSE $aggregate_version END,"
            "l.projection_owner=$owner "
            "RETURN true AS applied",
            id=str(lesson["id"]),
            lesson_key=lesson["lesson_key"],
            title=lesson["title"],
            status=lesson["status"],
            organization_id=lesson["organization_id"],
            package_sha256=lesson["package_sha256"],
            aggregate_version=aggregate_version,
            force=force,
            owner=owner,
        ).single()
        if applied is None or not bool(applied["applied"]):
            return False
        assertions_by_id = {str(assertion["id"]): assertion for assertion in lesson.get("assertions", [])}
        for relationship in Neo4jProjection.lesson_relationships(lesson):
            target_id = relationship["target_id"]
            if relationship["type"] == "GENERATED_ASSERTION":
                Neo4jProjection._project_assertion(session, assertions_by_id[target_id])
                session.run(
                    "MATCH (l:DesignLesson {id:$lesson_id}) "
                    "MERGE (a:KnowledgeAssertion {id:$assertion_id}) "
                    "ON CREATE SET a.projection_owner=$owner "
                    "MERGE (l)-[:GENERATED_ASSERTION]->(a)",
                    lesson_id=str(lesson["id"]),
                    assertion_id=target_id,
                    owner=owner,
                ).consume()
            elif relationship["type"] == "ORIGINATED_FROM":
                session.run(
                    "MATCH (l:DesignLesson {id:$lesson_id}) "
                    "MERGE (m:ModelRevision {id:$model_id}) "
                    "ON CREATE SET m.projection_owner=$owner "
                    "MERGE (l)-[:ORIGINATED_FROM]->(m)",
                    lesson_id=str(lesson["id"]),
                    model_id=target_id,
                    owner=owner,
                ).consume()
            elif relationship["type"] == "SUPERSEDES":
                session.run(
                    "MATCH (l:DesignLesson {id:$lesson_id}) "
                    "MERGE (previous:DesignLesson {id:$previous_lesson_id}) "
                    "ON CREATE SET previous.projection_owner=$owner "
                    "MERGE (l)-[:SUPERSEDES]->(previous)",
                    lesson_id=str(lesson["id"]),
                    previous_lesson_id=target_id,
                    owner=owner,
                ).consume()
        return True

    @staticmethod
    def _project_design_lesson_review(
        session: Any,
        review: dict[str, Any],
        *,
        occurred_at: Any,
        aggregate_version: int,
        force: bool = False,
    ) -> bool:
        """Project review workflow state as audit metadata only.

        Review nodes deliberately do not carry knowledge-assertion eligibility,
        scope, risk, applicability, or search properties.
        """
        occurred_at = Neo4jProjection._utc_iso8601_parameter(occurred_at)
        owner = "freecad-mechanical-design-agent"
        review_id = str(review["review_id"])
        applied = session.run(
            "MERGE (r:DesignLessonReview {review_id:$review_id}) "
            "ON CREATE SET r.aggregate_version=-1 "
            "WITH r WHERE $force OR coalesce(r.aggregate_version,-1) < $aggregate_version "
            "SET r.status=$status,r.working_copy_id=$working_copy_id,"
            "r.lesson_id=$lesson_id,r.updated_at=datetime($occurred_at),"
            "r.aggregate_version=CASE "
            "WHEN coalesce(r.aggregate_version,-1) > $aggregate_version "
            "THEN r.aggregate_version ELSE $aggregate_version END,"
            "r.projection_owner=$owner RETURN true AS applied",
            review_id=review_id,
            status=review["status"],
            working_copy_id=str(review["working_copy_id"]),
            lesson_id=str(review["lesson_id"]),
            occurred_at=occurred_at,
            aggregate_version=aggregate_version,
            force=force,
            owner=owner,
        ).single()
        if applied is None or not bool(applied["applied"]):
            return False
        published_design_lesson_id = review.get("published_design_lesson_id")
        if published_design_lesson_id is not None:
            session.run(
                "MATCH (r:DesignLessonReview {review_id:$review_id}) "
                "MERGE (l:DesignLesson {id:$published_design_lesson_id}) "
                "ON CREATE SET l.projection_owner=$owner "
                "MERGE (r)-[:PUBLISHED_AS]->(l)",
                review_id=review_id,
                published_design_lesson_id=str(published_design_lesson_id),
                owner=owner,
            ).consume()
        return True

    @staticmethod
    def _utc_iso8601_parameter(value: Any) -> str:
        """Keep native timezone objects outside the Neo4j Bolt encoder."""
        if not isinstance(value, str) or not value.strip():
            raise TypeError("Neo4j projection timestamps must be UTC ISO-8601 strings")
        candidate = value.strip()
        parseable = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        try:
            parsed = datetime.fromisoformat(parseable)
        except ValueError as exc:
            raise ValueError("Neo4j projection timestamp is not valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("Neo4j projection timestamp must use UTC")
        return candidate

    @staticmethod
    def _project_profile(session: Any, profile: dict[str, Any]) -> None:
        owner = "freecad-mechanical-design-agent"
        session.run(
            "MERGE (p:FamilyProfile {id:$id}) SET p.family_id=$family_id,p.revision=$revision,"
            "p.status=$status,p.distinct_model_count=$distinct_model_count,p.projection_owner=$owner",
            id=str(profile["id"]),
            family_id=profile["family_id"],
            revision=int(profile["revision"]),
            status=profile["status"],
            distinct_model_count=int(profile["distinct_model_count"]),
            owner=owner,
        ).consume()
        session.run(
            "MATCH (p:FamilyProfile {id:$profile_id}) MERGE (f:ProductFamily {id:$family_id}) "
            "ON CREATE SET f.projection_owner=$owner MERGE (p)-[:PROFILES]->(f)",
            profile_id=str(profile["id"]),
            family_id=profile["family_id"],
            owner=owner,
        ).consume()
