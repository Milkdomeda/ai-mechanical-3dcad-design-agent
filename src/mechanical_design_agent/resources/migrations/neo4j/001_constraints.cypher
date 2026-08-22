CREATE CONSTRAINT family_id_unique IF NOT EXISTS FOR (n:ProductFamily) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT product_id_unique IF NOT EXISTS FOR (n:Product) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT model_revision_id_unique IF NOT EXISTS FOR (n:ModelRevision) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT assertion_id_unique IF NOT EXISTS FOR (n:KnowledgeAssertion) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT source_node_key_unique IF NOT EXISTS FOR (n:SourceNode) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT family_profile_id_unique IF NOT EXISTS FOR (n:FamilyProfile) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT subfamily_id_unique IF NOT EXISTS FOR (n:ProductSubfamily) REQUIRE n.id IS UNIQUE;
