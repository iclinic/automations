<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * Um `addSql()` com **dois statements** separados por `;`. A unidade deste parser
 * é o statement e não a chamada, então saem dois findings — e o `DROP COLUMN`
 * não fica escondido atrás do `CREATE INDEX` que vem antes na mesma chamada.
 * É a mesma decisão do `queryRunner.query()` do TypeORM, e o oposto do
 * `op.execute()` do Alembic, que devolve o pior.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120400 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Indexa a transportadora e descarta o valor liquido';
    }

    public function up(Schema $schema): void
    {
        $this->addSql(<<<'SQL'
CREATE INDEX idx_freight_note_carrier ON stock."freight_note" (carrier_code);
ALTER TABLE stock."freight_note" DROP COLUMN net_amount;
SQL);
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE stock."freight_note" ADD COLUMN net_amount numeric(15,2) NULL');
        $this->addSql('DROP INDEX stock.idx_freight_note_carrier');
    }
}
