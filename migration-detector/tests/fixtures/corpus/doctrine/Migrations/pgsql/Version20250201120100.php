<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * A destruição no dialeto PostgreSQL, dentro de `up()`:
 * `ALTER COLUMN ... SET NOT NULL` — a forma que o MySQL escreve como
 * `MODIFY ... NOT NULL` — e `DROP COLUMN`. As duas formas do mesmo risco têm
 * que sair com a mesma cor nas duas bases do consumidor, e é este par com a
 * fixture MySQL irmã que prova isso.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120100 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Exige o rotulo da posicao e descarta a capacidade';
    }

    public function up(Schema $schema): void
    {
        $this->addSql('ALTER TABLE stock."warehouse_bin" ALTER COLUMN "bin_label" SET NOT NULL');
        $this->addSql('ALTER TABLE stock."warehouse_bin" DROP COLUMN "capacity"');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE stock."warehouse_bin" ADD COLUMN "capacity" integer NULL');
        $this->addSql('ALTER TABLE stock."warehouse_bin" ALTER COLUMN "bin_label" DROP NOT NULL');
    }
}
