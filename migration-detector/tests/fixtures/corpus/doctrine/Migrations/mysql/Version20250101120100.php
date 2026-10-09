<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * O `addSql()` com **parâmetros nomeados**, que é a forma que metade do corpus
 * real do consumidor usa. Duas coisas ficam presas aqui:
 *
 * - o segundo argumento — o array de parâmetros — nunca é lido, então nada do
 *   que ele carrega pode aparecer numa razão;
 * - `UPDATE` é migração de dados e sai `controlled`, com a tabela na razão e
 *   nenhum valor. É a mesma severidade do `RunPython` de dados do Django, e a
 *   mesma que o `RunSQL` do Django e o `op.execute()` do Alembic dão para o
 *   mesmo statement.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250101120100 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Acrescenta o nome da transportadora e preenche o das notas existentes';
    }

    public function up(Schema $schema): void
    {
        $this->addSql('ALTER TABLE freight_note ADD COLUMN carrier_name VARCHAR(120) NULL');

        $this->addSql(
            'UPDATE freight_note SET carrier_name = :name WHERE carrier_code = :code',
            [
                'name' => 'sem transportadora',
                'code' => 'none',
            ]
        );
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE freight_note DROP COLUMN carrier_name');
    }
}
