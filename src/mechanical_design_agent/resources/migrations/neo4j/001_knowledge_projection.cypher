CREATE CONSTRAINT product_family_id_unique IF NOT EXISTS
FOR (n:ProductFamily) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT knowledge_assertion_id_unique IF NOT EXISTS
FOR (n:KnowledgeAssertion) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT design_lesson_id_unique IF NOT EXISTS
FOR (n:DesignLesson) REQUIRE n.id IS UNIQUE;
