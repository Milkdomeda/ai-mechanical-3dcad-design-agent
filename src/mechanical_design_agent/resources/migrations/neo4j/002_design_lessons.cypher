CREATE CONSTRAINT design_lesson_id_unique IF NOT EXISTS
FOR (n:DesignLesson) REQUIRE n.id IS UNIQUE;
