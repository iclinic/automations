<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * SQL montado em tempo de execução por **concatenação**. O literal está ali e é
 * legível, mas não é o argumento inteiro: o pedaço que vem depois do `.` muda a
 * severidade, e é justamente o pedaço que não está escrito. A frase é a mesma
 * que o `op.execute(sql)` do Alembic e o template concatenado do TypeORM
 * recebem.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250101120500 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Redefine a nulidade do codigo da transportadora conforme o ambiente';
    }

    public function up(Schema $schema): void
    {
        $nullability = 'NOT NULL';

        $this->addSql('ALTER TABLE freight_note MODIFY carrier_code VARCHAR(40) ' . $nullability);
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE freight_note MODIFY carrier_code VARCHAR(40) NULL');
    }
}
