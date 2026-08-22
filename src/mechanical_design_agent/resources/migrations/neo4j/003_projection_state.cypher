CREATE CONSTRAINT projection_state_name_unique IF NOT EXISTS
FOR (state:ProjectionState) REQUIRE state.name IS UNIQUE;
