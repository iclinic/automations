<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * O **heredoc interpolado** (`<<<SQL`, sem as aspas no rótulo). O PHP interpola
 * o corpo, então o SQL que roda pode não ser o que está escrito, e classificar a
 * parte legível seria afirmar o que a interpolação pode desmentir. Resposta:
 * `unknown`.
 *
 * O corpus real do consumidor tem três nowdocs e nenhum heredoc interpolado; a
 * diferença entre as duas sintaxes é uma aspa no rótulo, e é por isso que ela
 * precisa de fixture.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250101120400 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Descarta a coluna que a configuracao indicar';
    }

    public function up(Schema $schema): void
    {
        $column = 'settled';

        $this->addSql(<<<SQL
ALTER TABLE freight_note DROP COLUMN {$column}
SQL);
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE freight_note ADD COLUMN settled TINYINT(1) NULL');
    }
}
