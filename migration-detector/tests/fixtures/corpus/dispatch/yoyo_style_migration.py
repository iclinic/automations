# yoyo-migrations. Casa `migration_paths`, declara operacoes e nao e nenhum dos
# stacks que o pacote conhece.
from yoyo import step

steps = [
    step("ALTER TABLE ledger_entry DROP COLUMN memo"),
]
